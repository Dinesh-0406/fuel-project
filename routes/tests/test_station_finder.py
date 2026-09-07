"""Corridor selection: which stations count as "on the way"."""

from __future__ import annotations

from decimal import Decimal

import pytest

from routes.models import FuelStation
from routes.services.geometry import RouteGeometry
from routes.services.station_finder import StationFinder
from routes.tests.conftest import straight_line

pytestmark = pytest.mark.django_db


@pytest.fixture
def north_south_route() -> RouteGeometry:
    """A meridian route from 40N to 45N at 75W, ~345 miles long."""
    return RouteGeometry(
        straight_line((40.0, -75.0), (45.0, -75.0), points=500),
        total_distance_miles=345.0,
        resample_miles=0.25,
        grid_cell_miles=25.0,
    )


def test_station_on_the_route_is_selected(make_station, north_south_route):
    make_station(latitude=42.5, longitude=-75.0, opis_id="on-route")
    result = StationFinder(corridor_miles=10.0).find(north_south_route)

    assert len(result.candidates) == 1
    candidate = result.candidates[0]
    assert candidate.offset_from_route_miles == pytest.approx(0.0, abs=0.3)
    assert candidate.distance_along_route_miles == pytest.approx(172.5, abs=3.0)


def test_station_outside_the_corridor_is_excluded(make_station, north_south_route):
    # ~1 degree of longitude at 42.5N is roughly 51 miles.
    make_station(latitude=42.5, longitude=-74.0, opis_id="far")
    result = StationFinder(corridor_miles=10.0).find(north_south_route)
    assert result.candidates == []


def test_widening_the_corridor_admits_more_stations(make_station, north_south_route):
    make_station(latitude=42.5, longitude=-74.9, opis_id="near")  # ~5 miles off
    make_station(latitude=42.5, longitude=-74.6, opis_id="mid")  # ~20 miles off

    narrow = StationFinder(corridor_miles=10.0).find(north_south_route)
    wide = StationFinder(corridor_miles=25.0).find(north_south_route)

    assert {c.opis_truckstop_id for c in narrow.candidates} == {"near"}
    assert {c.opis_truckstop_id for c in wide.candidates} == {"near", "mid"}
    assert narrow.corridor_miles == 10.0


def test_stations_far_from_the_route_never_leave_the_bounding_box(make_station, north_south_route):
    """A station in California must not even be fetched for an east-coast route."""
    make_station(latitude=34.05, longitude=-118.24, opis_id="la")
    make_station(latitude=42.5, longitude=-75.0, opis_id="on-route")

    result = StationFinder(corridor_miles=10.0).find(north_south_route)
    assert result.stations_in_bounding_box == 1  # only the on-route station was read
    assert [c.opis_truckstop_id for c in result.candidates] == ["on-route"]


def test_stations_without_coordinates_are_ignored(make_station, north_south_route):
    make_station(latitude=None, longitude=None, opis_id="ungeocoded")
    make_station(latitude=42.5, longitude=-75.0, opis_id="geocoded")

    result = StationFinder(corridor_miles=10.0).find(north_south_route)
    assert [c.opis_truckstop_id for c in result.candidates] == ["geocoded"]


def test_candidates_are_ordered_along_the_route(make_station, north_south_route):
    for latitude, name in [(44.0, "third"), (41.0, "first"), (43.0, "second")]:
        make_station(latitude=latitude, longitude=-75.0, opis_id=name)

    result = StationFinder(corridor_miles=10.0).find(north_south_route)
    assert [c.opis_truckstop_id for c in result.candidates] == ["first", "second", "third"]
    distances = [c.distance_along_route_miles for c in result.candidates]
    assert distances == sorted(distances)


def test_price_is_carried_through_as_decimal(make_station, north_south_route):
    make_station(latitude=42.5, longitude=-75.0, retail_price=Decimal("3.123456"))
    result = StationFinder(corridor_miles=10.0).find(north_south_route)
    assert isinstance(result.candidates[0].price, Decimal)
    assert result.candidates[0].price == Decimal("3.123456")


def test_duplicate_stations_in_one_town_are_all_returned(make_station, north_south_route):
    """Deduplication is the optimiser's job; the finder reports what exists."""
    make_station(latitude=42.5, longitude=-75.0, opis_id="a", retail_price=Decimal("3.10"))
    make_station(latitude=42.5, longitude=-75.0, opis_id="b", retail_price=Decimal("3.90"))

    result = StationFinder(corridor_miles=10.0).find(north_south_route)
    assert len(result.candidates) == 2


def test_bounding_box_query_is_a_single_database_hit(
    make_station, north_south_route, django_assert_num_queries
):
    for latitude in (41.0, 42.0, 43.0, 44.0):
        make_station(latitude=latitude, longitude=-75.0, opis_id=f"s{latitude}")

    with django_assert_num_queries(1):
        StationFinder(corridor_miles=10.0).find(north_south_route)


def test_large_station_set_is_processed_quickly(make_station, north_south_route):
    """Sanity check that the grid index keeps the search local, not quadratic."""
    import time

    FuelStation.objects.bulk_create(
        [
            FuelStation(
                opis_truckstop_id=f"bulk-{i}",
                truckstop_name=f"Bulk {i}",
                city="Testville",
                state="NY",
                retail_price=Decimal("3.50"),
                latitude=40.0 + (i % 500) * 0.01,
                longitude=-75.0 + ((i % 40) - 20) * 0.02,
            )
            for i in range(2000)
        ]
    )

    started = time.perf_counter()
    result = StationFinder(corridor_miles=10.0).find(north_south_route)
    elapsed = time.perf_counter() - started

    assert result.candidates, "expected some stations inside the corridor"
    assert elapsed < 2.0, f"corridor search took {elapsed:.2f}s for 2000 stations"
