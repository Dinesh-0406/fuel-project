"""Routing provider abstraction and the OSRM implementation.

Exactly one routing request is issued per uncached journey, and it asks for
alternatives: OSRM returns them in the same response, so several roads can be
costed against fuel prices without spending a second call. Each returned
polyline is then the sole geometric reference for finding stations near it --
the routing service is never consulted per station.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass

from django.conf import settings
from django.core.cache import cache

from routes.exceptions import NoRouteFound, ProviderUnavailable
from routes.services.cache import route_cache_key
from routes.services.geometry import Coordinate, meters_to_miles
from routes.services.http import ProviderHTTPError, get_json

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RouteResult:
    """A driving route between two points."""

    distance_miles: float
    duration_minutes: float
    coordinates: list[Coordinate]  # GeoJSON order: (longitude, latitude)
    provider: str

    @property
    def vertex_count(self) -> int:
        return len(self.coordinates)


class RoutingProvider(ABC):
    """Interface every routing backend must satisfy."""

    name: str = "unknown"

    @abstractmethod
    def route(self, start: tuple[float, float], finish: tuple[float, float]) -> RouteResult:
        """Return the driving route between two ``(latitude, longitude)`` points."""

    def routes(self, start: tuple[float, float], finish: tuple[float, float]) -> list[RouteResult]:
        """Every route worth costing, the provider's preferred one first.

        Fuel prices vary by region, so the shortest road is not always the
        cheapest one to drive. Providers that can offer alternatives override
        this; the rest simply offer the single route they have.
        """
        return [self.route(start, finish)]


class OSRMProvider(RoutingProvider):
    """Routing via an OSRM ``/route/v1/driving`` endpoint.

    OSRM was chosen because it is genuinely free with no API key, returns a
    full-precision GeoJSON LineString plus an authoritative driving distance in
    one call, and can be self-hosted unchanged for production use.
    """

    name = "OSRM"

    def __init__(
        self,
        base_url: str | None = None,
        timeout: float | None = None,
        max_retries: int | None = None,
    ) -> None:
        self.base_url = (base_url or settings.OSRM_BASE_URL).rstrip("/")
        self.timeout = timeout if timeout is not None else settings.OSRM_TIMEOUT_SECONDS
        self.max_retries = max_retries if max_retries is not None else settings.OSRM_MAX_RETRIES

    @staticmethod
    def _validate(point: tuple[float, float], label: str) -> tuple[float, float]:
        try:
            lat, lon = float(point[0]), float(point[1])
        except (TypeError, ValueError, IndexError) as exc:
            raise ProviderUnavailable(f"Malformed {label} coordinates.") from exc
        if not (-90.0 <= lat <= 90.0) or not (-180.0 <= lon <= 180.0):
            raise ProviderUnavailable(f"Malformed {label} coordinates.")
        return lat, lon

    def route(self, start: tuple[float, float], finish: tuple[float, float]) -> RouteResult:
        return self.routes(start, finish)[0]

    def routes(self, start: tuple[float, float], finish: tuple[float, float]) -> list[RouteResult]:
        start_lat, start_lon = self._validate(start, "start")
        finish_lat, finish_lon = self._validate(finish, "finish")

        url = (
            f"{self.base_url}/route/v1/driving/"
            f"{start_lon},{start_lat};{finish_lon},{finish_lat}"
        )
        try:
            payload = get_json(
                url,
                # Alternatives ride along in the same response, so costing several
                # roads still takes exactly one request.
                params={
                    "overview": "full",
                    "geometries": "geojson",
                    "steps": "false",
                    "alternatives": "true",
                },
                timeout=self.timeout,
                max_retries=self.max_retries,
                provider=self.name,
                headers={"User-Agent": settings.GEOCODER_USER_AGENT},
                # OSRM signals an impossible route with HTTP 400 and a JSON body
                # carrying code "NoRoute"; that is a routing answer, not an outage.
                payload_statuses={400},
            )
        except ProviderHTTPError as exc:
            raise ProviderUnavailable(str(exc)) from exc

        return self._parse(payload)

    def _parse(self, payload: dict) -> list[RouteResult]:
        if not isinstance(payload, dict):
            raise ProviderUnavailable("OSRM returned an unexpected response body.")

        code = payload.get("code")
        if code in {"NoRoute", "NoSegment"}:
            raise NoRouteFound("No drivable route exists between the supplied locations.")
        if code != "Ok":
            raise ProviderUnavailable(f"OSRM error: {code or 'unknown'}")

        routes = payload.get("routes") or []
        if not routes:
            raise NoRouteFound("No drivable route exists between the supplied locations.")

        parsed = [self._parse_route(route) for route in routes]
        logger.info(
            "Route resolved: %d option(s), %s",
            len(parsed),
            ", ".join(f"{r.distance_miles:.1f} mi" for r in parsed),
        )
        return parsed

    def _parse_route(self, route: dict) -> RouteResult:
        geometry = route.get("geometry") or {}
        coordinates = geometry.get("coordinates") or []
        if geometry.get("type") != "LineString" or len(coordinates) < 2:
            raise ProviderUnavailable("OSRM returned a route without usable geometry.")

        try:
            distance_meters = float(route["distance"])
            duration_seconds = float(route["duration"])
            parsed: list[Coordinate] = [(float(point[0]), float(point[1])) for point in coordinates]
        except (KeyError, TypeError, ValueError, IndexError) as exc:
            raise ProviderUnavailable("OSRM returned a malformed route.") from exc

        if distance_meters <= 0:
            raise NoRouteFound("The routing provider returned a zero-length route.")

        return RouteResult(
            distance_miles=meters_to_miles(distance_meters),
            duration_minutes=duration_seconds / 60.0,
            coordinates=parsed,
            provider=self.name,
        )


class CachedRoutingProvider(RoutingProvider):
    """Caches routes so a repeated request never re-hits the routing service."""

    def __init__(self, provider: RoutingProvider, ttl_seconds: int | None = None) -> None:
        self._provider = provider
        self.name = provider.name
        self.ttl = ttl_seconds if ttl_seconds is not None else settings.ROUTE_CACHE_TTL_SECONDS
        self.last_call_was_cached: bool | None = None

    def route(self, start: tuple[float, float], finish: tuple[float, float]) -> RouteResult:
        return self.routes(start, finish)[0]

    def routes(self, start: tuple[float, float], finish: tuple[float, float]) -> list[RouteResult]:
        key = route_cache_key(start, finish, self._provider.name)
        cached = cache.get(key)
        if cached is not None:
            self.last_call_was_cached = True
            logger.info("Route cache hit (%s)", key)
            return [RouteResult(**entry) for entry in cached]

        self.last_call_was_cached = False
        results = self._provider.routes(start, finish)
        cache.set(
            key,
            [
                {
                    "distance_miles": result.distance_miles,
                    "duration_minutes": result.duration_minutes,
                    "coordinates": result.coordinates,
                    "provider": result.provider,
                }
                for result in results
            ],
            self.ttl,
        )
        return results


def get_routing_provider() -> CachedRoutingProvider:
    """Factory used by the application layer."""
    return CachedRoutingProvider(OSRMProvider())
