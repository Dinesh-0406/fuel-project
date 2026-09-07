"""Geocoding provider abstraction for runtime (user-supplied) locations.

Scope note: this is used only for the two locations in an API request. Station
coordinates are NOT resolved here -- they are produced once by the offline
``enrich_fuel_stations`` command (see README). That separation is what keeps a
route request down to at most two geocoding calls instead of thousands.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass

from django.conf import settings
from django.core.cache import cache

from routes.exceptions import LocationNotFound, LocationOutsideServiceArea, ProviderUnavailable
from routes.services.cache import geocode_cache_key, normalize_location
from routes.services.http import ProviderHTTPError, get_json

logger = logging.getLogger(__name__)

# Generous bounds covering the 50 states plus territories, used as a cheap
# sanity check on whatever the provider returns.
US_BOUNDS = (17.5, -180.0, 72.0, -64.0)  # min_lat, min_lon, max_lat, max_lon


@dataclass(frozen=True)
class GeocodedLocation:
    query: str
    latitude: float
    longitude: float
    display_name: str = ""

    @property
    def as_tuple(self) -> tuple[float, float]:
        return self.latitude, self.longitude


def is_within_us(latitude: float, longitude: float) -> bool:
    min_lat, min_lon, max_lat, max_lon = US_BOUNDS
    return min_lat <= latitude <= max_lat and min_lon <= longitude <= max_lon


class GeocodingProvider(ABC):
    """Interface every geocoding backend must satisfy."""

    name: str = "unknown"

    @abstractmethod
    def geocode(self, location: str) -> GeocodedLocation:
        """Resolve a human-readable US location, or raise ``LocationNotFound``."""


class NominatimProvider(GeocodingProvider):
    """OpenStreetMap Nominatim, restricted to the United States.

    Suitable here because runtime geocoding is low volume (at most two calls per
    uncached request, and results are cached for 30 days). Passing
    ``countrycodes=us`` makes the provider itself reject non-US input, which is
    more reliable than pattern-matching the query string.
    """

    name = "Nominatim"

    def __init__(self, base_url: str | None = None, timeout: float | None = None) -> None:
        self.base_url = (base_url or settings.NOMINATIM_BASE_URL).rstrip("/")
        self.timeout = timeout if timeout is not None else settings.NOMINATIM_TIMEOUT_SECONDS

    def geocode(self, location: str) -> GeocodedLocation:
        query = normalize_location(location)
        if not query:
            raise LocationNotFound("An empty location cannot be geocoded.")

        try:
            payload = get_json(
                f"{self.base_url}/search",
                params={
                    "q": query,
                    "format": "jsonv2",
                    "limit": 1,
                    "countrycodes": "us",
                    "addressdetails": 0,
                },
                headers={"User-Agent": settings.GEOCODER_USER_AGENT},
                timeout=self.timeout,
                max_retries=1,
                provider=self.name,
            )
        except ProviderHTTPError as exc:
            raise ProviderUnavailable(str(exc)) from exc

        # Nominatim's /search returns a list; get_json is typed for JSON documents.
        results = payload if isinstance(payload, list) else payload.get("results", [])
        if not results:
            raise LocationNotFound(
                f"'{location}' could not be resolved to a location in the United States."
            )

        first = results[0]
        try:
            latitude = float(first["lat"])
            longitude = float(first["lon"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ProviderUnavailable("Geocoder returned a malformed result.") from exc

        if not is_within_us(latitude, longitude):
            raise LocationOutsideServiceArea(f"'{location}' resolved outside the United States.")

        return GeocodedLocation(
            query=location,
            latitude=latitude,
            longitude=longitude,
            display_name=str(first.get("display_name", "")),
        )


class CachedGeocoder(GeocodingProvider):
    """Caches geocoding by normalised location string.

    Negative results are cached too (for a shorter time) so a typo repeated in a
    loop cannot generate unlimited upstream traffic.
    """

    NEGATIVE_TTL_SECONDS = 600

    def __init__(self, provider: GeocodingProvider, ttl_seconds: int | None = None) -> None:
        self._provider = provider
        self.name = provider.name
        self.ttl = ttl_seconds if ttl_seconds is not None else settings.GEOCODE_CACHE_TTL_SECONDS
        self.call_count = 0

    def geocode(self, location: str) -> GeocodedLocation:
        key = geocode_cache_key(location)
        cached = cache.get(key)
        if cached is not None:
            if cached == "__notfound__":
                raise LocationNotFound(
                    f"'{location}' could not be resolved to a location in the " "United States."
                )
            logger.info("Geocode cache hit for %r", normalize_location(location))
            return GeocodedLocation(
                query=location,
                latitude=cached["latitude"],
                longitude=cached["longitude"],
                display_name=cached.get("display_name", ""),
            )

        self.call_count += 1
        try:
            result = self._provider.geocode(location)
        except LocationNotFound:
            cache.set(key, "__notfound__", self.NEGATIVE_TTL_SECONDS)
            raise

        cache.set(
            key,
            {
                "latitude": result.latitude,
                "longitude": result.longitude,
                "display_name": result.display_name,
            },
            self.ttl,
        )
        return result


def get_geocoding_provider() -> CachedGeocoder:
    """Factory used by the application layer."""
    return CachedGeocoder(NominatimProvider())
