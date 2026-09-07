"""Selects the fuel stations that lie within the route corridor.

Strategy (all local -- no network calls):

1. Take the route's bounding box and expand it by the corridor width.
2. Ask the database for stations inside that box. This is a single indexed
   query and typically cuts 6,700 stations down to a few hundred.
3. Project each surviving station onto the route polyline to get its exact
   perpendicular offset and its cumulative distance from the route start.
   ``RouteGeometry`` grid-indexes the route, so this step is O(1) per station
   rather than O(route vertices).

Step 1 alone is not enough: the bounding box of a long diagonal route contains
an enormous area the route never passes through, which is why step 3 exists.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from django.conf import settings

from routes.models import FuelStation
from routes.services.fuel_optimizer import CandidateStation
from routes.services.geometry import RouteGeometry

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class StationSearchResult:
    candidates: list[CandidateStation]
    stations_in_bounding_box: int
    corridor_miles: float


class StationFinder:
    def __init__(self, corridor_miles: float | None = None) -> None:
        self.corridor_miles = (
            corridor_miles if corridor_miles is not None else settings.FUEL_STATION_CORRIDOR_MILES
        )

    def find(self, route: RouteGeometry) -> StationSearchResult:
        box = route.bounding_box.expanded(self.corridor_miles)

        rows = list(
            FuelStation.objects.geocoded()
            .in_bounding_box(box.min_lat, box.min_lon, box.max_lat, box.max_lon)
            .values_list(
                "id",
                "opis_truckstop_id",
                "truckstop_name",
                "address",
                "city",
                "state",
                "latitude",
                "longitude",
                "retail_price",
            )
        )

        candidates: list[CandidateStation] = []
        for (
            pk,
            opis_id,
            name,
            address,
            city,
            state,
            latitude,
            longitude,
            price,
        ) in rows:
            position = route.locate(latitude, longitude, self.corridor_miles)
            if position is None:
                continue
            candidates.append(
                CandidateStation(
                    station_id=pk,
                    opis_truckstop_id=opis_id,
                    name=name,
                    address=address,
                    city=city,
                    state=state,
                    latitude=latitude,
                    longitude=longitude,
                    price=price,
                    distance_along_route_miles=position.along_miles,
                    offset_from_route_miles=position.offset_miles,
                )
            )

        candidates.sort(key=lambda c: c.distance_along_route_miles)
        logger.info(
            "Station search: %d in bounding box -> %d within %.1f-mile corridor",
            len(rows),
            len(candidates),
            self.corridor_miles,
        )
        return StationSearchResult(
            candidates=candidates,
            stations_in_bounding_box=len(rows),
            corridor_miles=self.corridor_miles,
        )
