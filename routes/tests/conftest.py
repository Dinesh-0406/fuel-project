"""Shared fixtures.

Nothing in this suite touches the network: the OSRM and Nominatim providers are
always replaced with fakes, so the tests run fully offline and deterministically.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from django.core.cache import cache

from routes.models import FuelStation
from routes.services.fuel_optimizer import CandidateStation
from routes.services.geocoding import GeocodedLocation, GeocodingProvider
from routes.services.routing import RouteResult, RoutingProvider


@pytest.fixture(autouse=True)
def clear_cache():
    """Every test starts with an empty cache so cache state never leaks."""
    cache.clear()
    yield
    cache.clear()


class FakeGeocoder(GeocodingProvider):
    """Returns canned coordinates and counts how many lookups it served."""

    name = "FakeGeocoder"

    def __init__(self, mapping: dict[str, tuple[float, float]] | None = None):
        self.mapping = mapping or {}
        self.calls: list[str] = []

    def geocode(self, location: str) -> GeocodedLocation:
        from routes.exceptions import LocationNotFound
        from routes.services.cache import normalize_location

        self.calls.append(location)
        key = normalize_location(location)
        if key not in self.mapping:
            raise LocationNotFound(f"'{location}' could not be resolved.")
        latitude, longitude = self.mapping[key]
        return GeocodedLocation(
            query=location, latitude=latitude, longitude=longitude, display_name=key
        )


class FakeRouter(RoutingProvider):
    """Serves a fixed route and records how many times it was asked."""

    name = "FakeRouter"

    def __init__(self, result: RouteResult):
        self.result = result
        self.calls = 0

    def route(self, start, finish) -> RouteResult:
        self.calls += 1
        return self.result


def straight_line(
    start: tuple[float, float], finish: tuple[float, float], points: int = 200
) -> list[tuple[float, float]]:
    """Interpolate a simple (lat, lon) -> GeoJSON (lon, lat) polyline."""
    start_lat, start_lon = start
    finish_lat, finish_lon = finish
    coordinates = []
    for i in range(points):
        t = i / (points - 1)
        coordinates.append(
            (start_lon + (finish_lon - start_lon) * t, start_lat + (finish_lat - start_lat) * t)
        )
    return coordinates


def make_candidate(
    position_miles: float,
    price: str,
    *,
    station_id: int = 0,
    offset_miles: float = 0.0,
    city: str = "Testville",
) -> CandidateStation:
    return CandidateStation(
        station_id=station_id or int(position_miles * 100) + 1,
        opis_truckstop_id=str(station_id or int(position_miles * 100) + 1),
        name=f"Station @{position_miles}",
        address="1 Test Way",
        city=city,
        state="TX",
        latitude=0.0,
        longitude=0.0,
        price=Decimal(price),
        distance_along_route_miles=float(position_miles),
        offset_from_route_miles=float(offset_miles),
    )


@pytest.fixture
def make_station(db):
    """Factory creating persisted FuelStation rows."""

    def _make(**kwargs) -> FuelStation:
        counter = FuelStation.objects.count() + 1
        defaults = {
            "opis_truckstop_id": str(kwargs.pop("opis_id", counter)),
            "truckstop_name": f"Station {counter}",
            "address": "I-10, EXIT 1",
            "city": "Testville",
            "state": "TX",
            "rack_id": "100",
            "retail_price": Decimal("3.500000"),
            "latitude": 0.0,
            "longitude": 0.0,
        }
        defaults.update(kwargs)
        return FuelStation.objects.create(**defaults)

    return _make
