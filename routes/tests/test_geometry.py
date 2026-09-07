"""Geometry and route-corridor maths."""

from __future__ import annotations

import pytest

from routes.services.geometry import (
    BoundingBox,
    RouteGeometry,
    haversine_miles,
    meters_to_miles,
)
from routes.tests.conftest import straight_line


def test_meters_to_miles_uses_the_exact_conversion():
    assert meters_to_miles(1609.344) == pytest.approx(1.0)
    assert meters_to_miles(1_272_199.4) == pytest.approx(790.5, abs=0.1)


def test_haversine_matches_a_known_distance():
    """New York -> Chicago great-circle distance is ~712 miles."""
    distance = haversine_miles(40.7128, -74.0060, 41.8781, -87.6298)
    assert distance == pytest.approx(712, abs=5)


def test_haversine_is_symmetric_and_zero_for_identical_points():
    assert haversine_miles(35.0, -90.0, 35.0, -90.0) == pytest.approx(0.0)
    assert haversine_miles(35.0, -90.0, 36.0, -91.0) == pytest.approx(
        haversine_miles(36.0, -91.0, 35.0, -90.0)
    )


def test_bounding_box_expansion_grows_in_both_axes():
    box = BoundingBox(min_lat=40.0, min_lon=-75.0, max_lat=41.0, max_lon=-74.0)
    grown = box.expanded(10.0)
    assert grown.min_lat < box.min_lat and grown.max_lat > box.max_lat
    assert grown.min_lon < box.min_lon and grown.max_lon > box.max_lon
    # 10 miles of latitude is about 0.145 degrees.
    assert (box.min_lat - grown.min_lat) == pytest.approx(0.1448, abs=0.002)


def test_route_geometry_rescales_to_the_authoritative_distance():
    """OSRM's distance wins over the polyline's own length."""
    coordinates = straight_line((40.0, -75.0), (41.0, -75.0), points=50)
    route = RouteGeometry(coordinates, total_distance_miles=100.0)
    assert route.cumulative_miles[-1] == pytest.approx(100.0)
    assert route.total_distance_miles == 100.0


def test_route_geometry_positions_a_point_on_the_route():
    coordinates = straight_line((40.0, -75.0), (41.0, -75.0), points=200)
    route = RouteGeometry(coordinates, total_distance_miles=69.0)

    # Halfway along the meridian.
    position = route.locate(40.5, -75.0, max_offset_miles=10.0)
    assert position is not None
    assert position.offset_miles == pytest.approx(0.0, abs=0.2)
    assert position.along_miles == pytest.approx(34.5, abs=0.5)


def test_route_geometry_rejects_points_outside_the_corridor():
    coordinates = straight_line((40.0, -75.0), (41.0, -75.0), points=200)
    route = RouteGeometry(coordinates, total_distance_miles=69.0)
    # ~53 miles east at this latitude.
    assert route.locate(40.5, -74.0, max_offset_miles=10.0) is None
    assert route.locate(40.5, -74.0, max_offset_miles=60.0) is not None


def test_route_geometry_measures_perpendicular_offset():
    coordinates = straight_line((40.0, -75.0), (41.0, -75.0), points=200)
    route = RouteGeometry(coordinates, total_distance_miles=69.0)
    # 0.1 degrees of longitude at 40.5N is ~5.3 miles.
    position = route.locate(40.5, -74.9, max_offset_miles=20.0)
    assert position is not None
    assert position.offset_miles == pytest.approx(5.26, abs=0.3)


def test_route_geometry_resampling_reduces_vertex_count():
    coordinates = straight_line((40.0, -75.0), (45.0, -75.0), points=5000)
    route = RouteGeometry(coordinates, total_distance_miles=345.0, resample_miles=1.0)
    assert route.vertex_count == 5000
    assert route.resampled_vertex_count < 400
    # Endpoints are always retained.
    assert route.lats[0] == pytest.approx(40.0)
    assert route.lats[-1] == pytest.approx(45.0)


def test_route_geometry_requires_two_points():
    with pytest.raises(ValueError):
        RouteGeometry([(-75.0, 40.0)], total_distance_miles=1.0)


def test_locate_is_accurate_on_a_dog_leg_route():
    """A right-angled route: a point at the corner maps to the corner."""
    leg_one = straight_line((40.0, -75.0), (41.0, -75.0), points=100)
    leg_two = straight_line((41.0, -75.0), (41.0, -73.0), points=100)
    route = RouteGeometry(leg_one + leg_two, total_distance_miles=173.0, resample_miles=0.25)

    position = route.locate(41.0, -75.0, max_offset_miles=5.0)
    assert position is not None
    assert position.offset_miles == pytest.approx(0.0, abs=0.5)
    # The corner sits at the end of leg one: 69 of 173 miles.
    assert position.along_miles == pytest.approx(69.0, abs=2.0)


def test_resampling_trades_corner_accuracy_for_speed():
    """Coarser resampling cuts corners -- bounded by the resample spacing.

    This is the documented trade-off behind ROUTE_RESAMPLE_MILES: at the default
    1-mile spacing a sharp corner can be cut by up to about that distance, which
    is far below the accuracy of the station coordinates themselves.
    """
    leg_one = straight_line((40.0, -75.0), (41.0, -75.0), points=100)
    leg_two = straight_line((41.0, -75.0), (41.0, -73.0), points=100)
    corner = (41.0, -75.0)

    fine = RouteGeometry(leg_one + leg_two, 173.0, resample_miles=0.25)
    coarse = RouteGeometry(leg_one + leg_two, 173.0, resample_miles=1.0)

    fine_offset = fine.locate(*corner, max_offset_miles=10.0).offset_miles
    coarse_offset = coarse.locate(*corner, max_offset_miles=10.0).offset_miles

    assert fine_offset <= coarse_offset
    assert coarse_offset < 1.5  # bounded by the resample spacing
    assert coarse.resampled_vertex_count < fine.resampled_vertex_count


def test_long_sparse_segments_are_still_found():
    """A segment spanning several grid cells must not fall through the index.

    OSRM emits sparse vertices on long straight motorway runs, so a single
    segment can be far longer than one grid cell. The index must record it in
    every cell it crosses.
    """
    # Two points ~200 miles apart: one segment crossing many 10-mile cells.
    route = RouteGeometry(
        [(-75.0, 40.0), (-75.0, 43.0)],
        total_distance_miles=207.0,
        resample_miles=1.0,
        grid_cell_miles=10.0,
    )
    # A station in the middle of that long segment, right on the line.
    position = route.locate(41.5, -75.0, max_offset_miles=10.0)
    assert position is not None
    assert position.offset_miles == pytest.approx(0.0, abs=0.5)
    assert position.along_miles == pytest.approx(103.5, abs=2.0)
