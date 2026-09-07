"""Routing provider abstraction and the OSRM implementation.

Exactly one routing request is issued per uncached route. The returned polyline
is then used as the sole geometric reference for finding fuel stations -- the
routing service is never consulted per station.
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
        start_lat, start_lon = self._validate(start, "start")
        finish_lat, finish_lon = self._validate(finish, "finish")

        url = (
            f"{self.base_url}/route/v1/driving/"
            f"{start_lon},{start_lat};{finish_lon},{finish_lat}"
        )
        try:
            payload = get_json(
                url,
                params={"overview": "full", "geometries": "geojson", "steps": "false"},
                timeout=self.timeout,
                max_retries=self.max_retries,
                provider=self.name,
                headers={"User-Agent": settings.GEOCODER_USER_AGENT},
            )
        except ProviderHTTPError as exc:
            raise ProviderUnavailable(str(exc)) from exc

        return self._parse(payload)

    def _parse(self, payload: dict) -> RouteResult:
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

        route = routes[0]
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

        result = RouteResult(
            distance_miles=meters_to_miles(distance_meters),
            duration_minutes=duration_seconds / 60.0,
            coordinates=parsed,
            provider=self.name,
        )
        logger.info(
            "Route resolved: %.1f miles, %.0f minutes, %d vertices",
            result.distance_miles,
            result.duration_minutes,
            result.vertex_count,
        )
        return result


class CachedRoutingProvider(RoutingProvider):
    """Caches routes so a repeated request never re-hits the routing service."""

    def __init__(self, provider: RoutingProvider, ttl_seconds: int | None = None) -> None:
        self._provider = provider
        self.name = provider.name
        self.ttl = ttl_seconds if ttl_seconds is not None else settings.ROUTE_CACHE_TTL_SECONDS
        self.last_call_was_cached: bool | None = None

    def route(self, start: tuple[float, float], finish: tuple[float, float]) -> RouteResult:
        key = route_cache_key(start, finish, self._provider.name)
        cached = cache.get(key)
        if cached is not None:
            self.last_call_was_cached = True
            logger.info("Route cache hit (%s)", key)
            return RouteResult(**cached)

        self.last_call_was_cached = False
        result = self._provider.route(start, finish)
        cache.set(
            key,
            {
                "distance_miles": result.distance_miles,
                "duration_minutes": result.duration_minutes,
                "coordinates": result.coordinates,
                "provider": result.provider,
            },
            self.ttl,
        )
        return result


def get_routing_provider() -> CachedRoutingProvider:
    """Factory used by the application layer."""
    return CachedRoutingProvider(OSRMProvider())
