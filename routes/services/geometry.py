"""Local geometry helpers for route/station spatial reasoning.

Everything here is pure computation -- no I/O, no network. The route returned by
OSRM is the single geometric reference used to decide which stations are on the
way and how far along the route each one sits.

Projection assumption
---------------------
Distances are computed with the haversine formula on a spherical Earth. For the
point-to-segment work inside the corridor we switch to a local equirectangular
projection anchored at the segment: over the tens of miles involved the error of
that approximation is well under 0.1%, far below the accuracy of the station
coordinates themselves (see README, "Known limitations").
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

EARTH_RADIUS_MILES = 3958.7613
MILES_PER_DEGREE_LAT = math.pi * EARTH_RADIUS_MILES / 180.0  # ~69.09
METERS_PER_MILE = 1609.344

Coordinate = tuple[float, float]  # (longitude, latitude), GeoJSON order


def meters_to_miles(meters: float) -> float:
    return meters / METERS_PER_MILE


def haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in miles between two WGS84 points."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = phi2 - phi1
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2.0) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2.0) ** 2
    return 2.0 * EARTH_RADIUS_MILES * math.asin(math.sqrt(min(1.0, a)))


def miles_to_degrees_lat(miles: float) -> float:
    return miles / MILES_PER_DEGREE_LAT


def miles_to_degrees_lon(miles: float, at_latitude: float) -> float:
    """Longitude degrees spanning ``miles`` at the given latitude."""
    cos_lat = max(math.cos(math.radians(at_latitude)), 0.01)
    return miles / (MILES_PER_DEGREE_LAT * cos_lat)


@dataclass(frozen=True)
class BoundingBox:
    min_lat: float
    min_lon: float
    max_lat: float
    max_lon: float

    def expanded(self, miles: float) -> BoundingBox:
        """Grow the box by ``miles`` on every side.

        Longitude growth uses the latitude furthest from the equator so the box
        is never too narrow anywhere along the route.
        """
        widest_lat = max(abs(self.min_lat), abs(self.max_lat))
        dlat = miles_to_degrees_lat(miles)
        dlon = miles_to_degrees_lon(miles, widest_lat)
        return BoundingBox(
            min_lat=max(self.min_lat - dlat, -90.0),
            min_lon=max(self.min_lon - dlon, -180.0),
            max_lat=min(self.max_lat + dlat, 90.0),
            max_lon=min(self.max_lon + dlon, 180.0),
        )


@dataclass(frozen=True)
class RoutePosition:
    """Where a point sits relative to the route."""

    offset_miles: float  # perpendicular distance from the route
    along_miles: float  # cumulative driving distance from the route start


def _segment_offset(
    lat: float, lon: float, alat: float, alon: float, blat: float, blon: float
) -> tuple[float, float]:
    """Distance in miles from P to segment AB, plus the projection fraction t.

    Uses a local equirectangular projection anchored at A.
    """
    cos_lat = math.cos(math.radians(alat))
    ax, ay = 0.0, 0.0
    bx = (blon - alon) * cos_lat * MILES_PER_DEGREE_LAT
    by = (blat - alat) * MILES_PER_DEGREE_LAT
    px = (lon - alon) * cos_lat * MILES_PER_DEGREE_LAT
    py = (lat - alat) * MILES_PER_DEGREE_LAT

    dx, dy = bx - ax, by - ay
    length_sq = dx * dx + dy * dy
    if length_sq <= 1e-12:
        return math.hypot(px, py), 0.0

    t = max(0.0, min(1.0, (px * dx + py * dy) / length_sq))
    cx, cy = dx * t, dy * t
    return math.hypot(px - cx, py - cy), t


class RouteGeometry:
    """A route polyline prepared for fast repeated proximity queries.

    Construction does three things:

    1. Computes the cumulative driving distance at every vertex, then rescales it
       so the final value equals OSRM's authoritative route distance.
    2. Resamples the polyline down to ``resample_miles`` spacing. OSRM returns
       tens of thousands of vertices for a long route; testing every station
       against every vertex is the O(stations x vertices) blow-up we want to
       avoid. Each retained vertex keeps its exact cumulative distance.
    3. Builds a uniform grid index over the retained segments, so a station only
       ever tests the handful of segments in its own and neighbouring cells.

    The result is that ``locate()`` is effectively O(1) per station.
    """

    def __init__(
        self,
        coordinates: Sequence[Coordinate],
        total_distance_miles: float,
        *,
        resample_miles: float = 1.0,
        grid_cell_miles: float = 10.0,
    ) -> None:
        if len(coordinates) < 2:
            raise ValueError("A route needs at least two coordinates.")

        self.total_distance_miles = float(total_distance_miles)
        self._grid_cell_miles = max(float(grid_cell_miles), 1.0)

        lats = [float(c[1]) for c in coordinates]
        lons = [float(c[0]) for c in coordinates]

        # 1. cumulative distance along the raw polyline
        cumulative = [0.0] * len(coordinates)
        for i in range(1, len(coordinates)):
            cumulative[i] = cumulative[i - 1] + haversine_miles(
                lats[i - 1], lons[i - 1], lats[i], lons[i]
            )
        polyline_miles = cumulative[-1]

        # OSRM's distance is authoritative; rescale so the two agree exactly.
        scale = self.total_distance_miles / polyline_miles if polyline_miles > 0.0 else 1.0
        self.polyline_miles = polyline_miles

        # 2. resample
        keep_lat: list[float] = [lats[0]]
        keep_lon: list[float] = [lons[0]]
        keep_cum: list[float] = [0.0]
        last = 0.0
        for i in range(1, len(coordinates) - 1):
            if cumulative[i] - last >= resample_miles:
                keep_lat.append(lats[i])
                keep_lon.append(lons[i])
                keep_cum.append(cumulative[i] * scale)
                last = cumulative[i]
        keep_lat.append(lats[-1])
        keep_lon.append(lons[-1])
        keep_cum.append(self.total_distance_miles)

        self.lats = keep_lat
        self.lons = keep_lon
        self.cumulative_miles = keep_cum
        self.vertex_count = len(coordinates)
        self.resampled_vertex_count = len(keep_lat)

        self.bounding_box = BoundingBox(
            min_lat=min(lats), min_lon=min(lons), max_lat=max(lats), max_lon=max(lons)
        )

        # 3. grid index over segments
        self._cell_lat = miles_to_degrees_lat(self._grid_cell_miles)
        mid_lat = (self.bounding_box.min_lat + self.bounding_box.max_lat) / 2.0
        self._cell_lon = miles_to_degrees_lon(self._grid_cell_miles, mid_lat)
        self._grid: dict[tuple[int, int], list[int]] = {}
        for i in range(len(keep_lat) - 1):
            # Index the segment into EVERY cell its bounding box touches, not
            # just its endpoints' cells. Resampling guarantees a minimum spacing
            # but not a maximum -- OSRM emits sparse vertices on long straight
            # motorway runs -- so a segment can span several cells, and indexing
            # only the endpoints would leave the cells in between blind to it.
            a_lat, a_lon = self._cell_of(keep_lat[i], keep_lon[i])
            b_lat, b_lon = self._cell_of(keep_lat[i + 1], keep_lon[i + 1])
            for cell_lat in range(min(a_lat, b_lat), max(a_lat, b_lat) + 1):
                for cell_lon in range(min(a_lon, b_lon), max(a_lon, b_lon) + 1):
                    self._grid.setdefault((cell_lat, cell_lon), []).append(i)

    def _cell_of(self, lat: float, lon: float) -> tuple[int, int]:
        return (int(math.floor(lat / self._cell_lat)), int(math.floor(lon / self._cell_lon)))

    @property
    def start(self) -> tuple[float, float]:
        return self.lats[0], self.lons[0]

    @property
    def finish(self) -> tuple[float, float]:
        return self.lats[-1], self.lons[-1]

    def locate(self, lat: float, lon: float, max_offset_miles: float) -> RoutePosition | None:
        """Position a point against the route.

        Returns ``None`` when the point lies further than ``max_offset_miles``
        from the route. Otherwise returns its perpendicular offset and the
        cumulative driving distance at the projection point.
        """
        cell_lat, cell_lon = self._cell_of(lat, lon)
        # One ring of neighbours is enough while max_offset <= grid cell size.
        reach = max(1, math.ceil(max_offset_miles / self._grid_cell_miles))

        best_offset = math.inf
        best_along = 0.0
        seen: set[int] = set()
        for dlat in range(-reach, reach + 1):
            for dlon in range(-reach, reach + 1):
                for i in self._grid.get((cell_lat + dlat, cell_lon + dlon), ()):
                    if i in seen:
                        continue
                    seen.add(i)
                    offset, t = _segment_offset(
                        lat,
                        lon,
                        self.lats[i],
                        self.lons[i],
                        self.lats[i + 1],
                        self.lons[i + 1],
                    )
                    if offset < best_offset:
                        best_offset = offset
                        best_along = self.cumulative_miles[i] + t * (
                            self.cumulative_miles[i + 1] - self.cumulative_miles[i]
                        )

        if best_offset > max_offset_miles:
            return None
        return RoutePosition(offset_miles=best_offset, along_miles=best_along)


def as_linestring(coordinates: Iterable[Coordinate]) -> dict:
    """Wrap raw coordinates in a GeoJSON LineString."""
    return {"type": "LineString", "coordinates": [[float(x), float(y)] for x, y in coordinates]}
