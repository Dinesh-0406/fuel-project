"""Cost-optimal fuel stop selection along a route.

The problem
-----------
Stations sit at known distances along the route, each with a price per gallon.
The vehicle starts with a full tank, burns fuel at a fixed MPG and can never
hold more than ``max_range_miles / mpg`` gallons. Choose where to stop and how
many gallons to buy at each stop so that the destination is reached and the
total spend is minimised.

Reaching a station is not free
-----------------------------
This is the classic *gas station problem* with one addition that changes it:
stations sit up to the corridor width off the route, so stopping costs the round
trip out and back. Without that term the planner will chase a cent of price into
a dollar of driving -- filling at the cheapest station, then topping up 0.2
gallons two miles later at a station 2.3 miles off the highway, to save $0.01.

Ignoring the detour is not only a pricing error. Those miles are really driven,
so a plan that budgets fuel for the route alone sends the vehicle out short.

A fixed cost per stop breaks the textbook greedy -- which station to visit stops
being a local decision -- so the plan is solved exactly instead, by dynamic
programming over (junction, fuel level). The solver knows only two moves, *buy
one unit of range* and *drive to the next junction*, and evaluates every
reachable state, so it assumes nothing about what a good answer looks like. It
is O(stations x tank units) and does no I/O: about 40ms for a coast-to-coast
route with 460 candidate stations.

Optimality is not taken on faith. ``tests/test_optimizer_optimality.py`` checks
the plan against an independent oracle over every instance in several bounded
universes -- some 46,000 of them -- plus randomised and adversarial ones.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal

logger = logging.getLogger(__name__)

CENTS = Decimal("0.01")
# Physical quantities that are compared against distances carry a small epsilon
# so that "exactly 500 miles" is treated as reachable despite float arithmetic.
EPSILON_MILES = 1e-6
# Fuel is planned in whole units of this many miles of range. Finer grids cost
# time linearly and buy less and less: on a coast-to-coast route, going from a
# half mile to a twentieth only moves the plan by about a dollar in seven
# hundred, and routes are resampled at one-mile spacing before stations are
# projected onto them, so the positions are not that precise to begin with.
RESOLUTION_MILES = 0.25


@dataclass(frozen=True)
class CandidateStation:
    """A station that lies inside the route corridor, positioned along the route."""

    station_id: int
    opis_truckstop_id: str
    name: str
    address: str
    city: str
    state: str
    latitude: float
    longitude: float
    price: Decimal
    distance_along_route_miles: float
    offset_from_route_miles: float


@dataclass(frozen=True)
class FuelStop:
    sequence: int
    station: CandidateStation
    price_per_gallon: Decimal
    distance_from_start_miles: float
    distance_from_previous_stop_miles: float
    distance_to_destination_miles: float
    fuel_before_purchase_gallons: float
    fuel_purchased_gallons: float
    fuel_after_purchase_gallons: float
    cost: Decimal


@dataclass(frozen=True)
class FuelPlan:
    stops: list[FuelStop] = field(default_factory=list)
    total_cost: Decimal = Decimal("0.00")
    total_gallons_purchased: float = 0.0
    total_fuel_consumed_gallons: float = 0.0
    starting_fuel_gallons: float = 0.0
    fuel_remaining_at_destination_gallons: float = 0.0


class InfeasiblePlan(Exception):
    """Raised when no ordering of the available stations can reach the destination."""

    def __init__(self, message: str, details: dict | None = None):
        super().__init__(message)
        self.details = details or {}


class FuelPlanOptimizer:
    """Selects cost-optimal fuel stops for a fixed vehicle configuration."""

    def __init__(
        self,
        max_range_miles: float,
        mpg: float,
        *,
        reserve_gallons: float = 0.0,
        resolution_miles: float = RESOLUTION_MILES,
    ) -> None:
        if max_range_miles <= 0 or mpg <= 0:
            raise ValueError("max_range_miles and mpg must be positive.")
        if resolution_miles <= 0:
            raise ValueError("resolution_miles must be positive.")
        self.max_range_miles = float(max_range_miles)
        self.mpg = float(mpg)
        self.resolution_miles = float(resolution_miles)
        self.tank_capacity_gallons = self.max_range_miles / self.mpg
        # The reserve is a safety margin against imprecise station positions: no
        # leg is planned longer than the reduced range, and every purchase is
        # sized so the vehicle still arrives holding the reserve.
        self.reserve_gallons = max(0.0, float(reserve_gallons))
        self.usable_range_miles = self.range_from(self.tank_capacity_gallons)

    def range_from(self, gallons: float) -> float:
        """Miles drivable on ``gallons`` while still keeping the reserve intact."""
        return max(0.0, gallons - self.reserve_gallons) * self.mpg

    # -- public API ---------------------------------------------------------

    def plan(
        self,
        candidates: Sequence[CandidateStation],
        total_route_miles: float,
        *,
        starting_fuel_gallons: float | None = None,
    ) -> FuelPlan:
        """Build the cheapest feasible fuel plan, or raise ``InfeasiblePlan``."""
        capacity = self.tank_capacity_gallons
        start_fuel = capacity if starting_fuel_gallons is None else float(starting_fuel_gallons)
        start_fuel = min(max(start_fuel, 0.0), capacity)
        destination = float(total_route_miles)

        stations = self._prepare(candidates, destination)
        self._assert_feasible(stations, destination, start_fuel)

        stops = self._solve(stations, destination, start_fuel)

        total_cost = sum((s.cost for s in stops), Decimal("0.00"))
        total_gallons = sum(s.fuel_purchased_gallons for s in stops)
        detour_miles = sum(2.0 * s.station.offset_from_route_miles for s in stops)
        consumed = (destination + detour_miles) / self.mpg
        remaining = start_fuel + total_gallons - consumed

        logger.info(
            "Fuel plan: %d stop(s), %.3f gal purchased, $%s total over %.1f miles "
            "(+%.1f mi of detours)",
            len(stops),
            total_gallons,
            total_cost,
            destination,
            detour_miles,
        )
        return FuelPlan(
            stops=stops,
            total_cost=total_cost,
            total_gallons_purchased=total_gallons,
            total_fuel_consumed_gallons=consumed,
            starting_fuel_gallons=start_fuel,
            fuel_remaining_at_destination_gallons=max(0.0, remaining),
        )

    # -- internals ----------------------------------------------------------

    @staticmethod
    def _prepare(
        candidates: Sequence[CandidateStation], destination: float
    ) -> list[CandidateStation]:
        """Sort by position and drop stations that cannot be part of a plan.

        Stations at or beyond the destination are useless, and when several
        stations sit at the same point along the route only the cheapest can
        ever be chosen.
        """
        usable = [
            c
            for c in candidates
            if -EPSILON_MILES <= c.distance_along_route_miles <= destination + EPSILON_MILES
        ]
        usable.sort(
            key=lambda c: (c.distance_along_route_miles, c.price, c.offset_from_route_miles)
        )

        deduped: list[CandidateStation] = []
        for station in usable:
            if deduped:
                twin = deduped[-1]
                same_point = (
                    abs(station.distance_along_route_miles - twin.distance_along_route_miles)
                    < EPSILON_MILES
                )
                # Only a station that is no cheaper AND no closer to the route is
                # truly redundant. Dropping on price alone would discard a
                # roadside station in favour of a fractionally cheaper one miles
                # off the route.
                if same_point and station.offset_from_route_miles >= twin.offset_from_route_miles:
                    continue
            deduped.append(station)
        return deduped

    def _assert_feasible(
        self, stations: Sequence[CandidateStation], destination: float, start_fuel: float
    ) -> None:
        """Forward reachability scan.

        Walks the stations in order, tracking the furthest point reachable if the
        tank were filled at every station passed so far. If the destination is
        not covered, no strategy can succeed and the greedy would be chasing an
        impossible plan.
        """
        reachable = self.range_from(start_fuel)
        if destination <= reachable + EPSILON_MILES:
            return

        first_gap_at = None
        for station in stations:
            if station.distance_along_route_miles > reachable + EPSILON_MILES:
                first_gap_at = station.distance_along_route_miles
                break
            reachable = max(reachable, station.distance_along_route_miles + self.usable_range_miles)
            if destination <= reachable + EPSILON_MILES:
                return

        details = {
            "total_route_miles": round(destination, 2),
            "vehicle_range_miles": round(self.usable_range_miles, 2),
            "stations_on_route": len(stations),
            "furthest_reachable_mile": round(reachable, 2),
        }
        if first_gap_at is not None:
            details["first_unreachable_station_mile"] = round(first_gap_at, 2)
        raise InfeasiblePlan(
            "No feasible fuel plan was found within the vehicle's "
            f"{self.usable_range_miles:.0f}-mile range using the available fuel "
            "station dataset.",
            details,
        )

    # -- exact solver -------------------------------------------------------

    def _solve(
        self, stations: Sequence[CandidateStation], destination: float, start_fuel: float
    ) -> list[FuelStop]:
        """Cheapest way to reach the destination, detours included.

        Fuel is measured in whole ``RESOLUTION_MILES`` units of range, which
        turns a continuous problem into a finite one without pretending to a
        precision the inputs do not have: station positions come from a route
        resampled at one-mile spacing, so a half-mile grid is already finer than
        the data underneath it.

        Only two moves exist -- *buy one unit of range* and *drive to the next
        junction* -- and every reachable state is evaluated, so nothing about the
        shape of a good answer is assumed. Stopping costs the round trip off the
        route, which is what stops the planner chasing a cent of price into a
        dollar of driving.
        """
        unit = self.resolution_miles
        capacity = int(self.usable_range_miles / unit + EPSILON_MILES)
        start_units = min(int(self.range_from(start_fuel) / unit + EPSILON_MILES), capacity)

        count = len(stations)
        # Positions are rounded (not floored) so the rounding error telescopes
        # away across the route instead of accumulating leg by leg. Detours are
        # rounded up, always charging the driver for the whole round trip.
        junctions = [0] + [round(s.distance_along_route_miles / unit) for s in stations]
        finish = round(destination / unit)
        legs = [
            (junctions[k + 1] if k + 1 <= count else finish) - junctions[k]
            for k in range(count + 1)
        ]
        approach = [math.ceil(s.offset_from_route_miles / unit - EPSILON_MILES) for s in stations]
        unit_price = [float(s.price) * unit / self.mpg for s in stations]

        # Cost to finish, per fuel level, walking backwards from the destination.
        arrived = [0.0] * (capacity + 1)
        onward: list[list[float]] = [arrived]
        at_pump: list[list[float] | None] = [None] * (count + 1)

        for node in range(count, -1, -1):
            leg = legs[node]
            ahead = onward[0]

            if node >= 1:
                index = node - 1
                detour_out = approach[index] + leg
                price = unit_price[index]
                pump = [
                    ahead[fuel - detour_out] if fuel >= detour_out else math.inf
                    for fuel in range(capacity + 1)
                ]
                for fuel in range(capacity - 1, -1, -1):
                    buying = price + pump[fuel + 1]
                    if buying < pump[fuel]:
                        pump[fuel] = buying
                at_pump[node] = pump

            here = [math.inf] * (capacity + 1)
            for fuel in range(capacity + 1):
                best = ahead[fuel - leg] if fuel >= leg else math.inf
                if node >= 1 and fuel >= approach[node - 1]:
                    # Ties go to driving on: an equal-priced stop is pure hassle.
                    stopping = at_pump[node][fuel - approach[node - 1]]
                    if stopping < best:
                        best = stopping
                here[fuel] = best
            onward.insert(0, here)

        if math.isinf(onward[0][start_units]):
            raise InfeasiblePlan(
                "No feasible fuel plan was found within the vehicle's "
                f"{self.usable_range_miles:.0f}-mile range once the detour to each "
                "station is accounted for.",
                {
                    "total_route_miles": round(destination, 2),
                    "vehicle_range_miles": round(self.usable_range_miles, 2),
                    "stations_on_route": count,
                },
            )

        return self._replay(
            stations,
            destination,
            onward,
            at_pump,
            legs,
            approach,
            unit_price,
            start_units,
            capacity,
        )

    def _replay(
        self,
        stations: Sequence[CandidateStation],
        destination: float,
        onward: list[list[float]],
        at_pump: list[list[float] | None],
        legs: list[int],
        approach: list[int],
        unit_price: list[float],
        start_units: int,
        capacity: int,
    ) -> list[FuelStop]:
        """Walk the solved table forwards, turning decisions into stops."""
        unit = self.resolution_miles
        stops: list[FuelStop] = []
        fuel = start_units
        previous_stop_mile = 0.0

        def gallons(units: int) -> float:
            return units * unit / self.mpg + self.reserve_gallons

        for node in range(len(legs)):
            leg = legs[node]
            ahead = onward[node + 1]
            driving_on = ahead[fuel - leg] if fuel >= leg else math.inf

            if node == 0:
                fuel -= leg
                continue

            index = node - 1
            reach = approach[index]
            stopping = at_pump[node][fuel - reach] if fuel >= reach else math.inf
            if not stopping < driving_on:
                fuel -= leg
                continue

            station = stations[index]
            pump = at_pump[node]
            price = unit_price[index]
            detour_out = reach + leg

            before = fuel - reach
            held = before
            while held < capacity:
                leaving = ahead[held - detour_out] if held >= detour_out else math.inf
                if leaving <= price + pump[held + 1]:
                    break
                held += 1

            purchased = gallons(held) - gallons(before)
            position = station.distance_along_route_miles
            if purchased > 0.0:
                stops.append(
                    FuelStop(
                        sequence=len(stops) + 1,
                        station=station,
                        price_per_gallon=station.price,
                        distance_from_start_miles=position,
                        distance_from_previous_stop_miles=position - previous_stop_mile,
                        distance_to_destination_miles=destination - position,
                        fuel_before_purchase_gallons=gallons(before),
                        fuel_purchased_gallons=purchased,
                        fuel_after_purchase_gallons=gallons(held),
                        cost=(Decimal(str(purchased)) * station.price).quantize(
                            CENTS, rounding=ROUND_HALF_UP
                        ),
                    )
                )
                previous_stop_mile = position

            fuel = held - detour_out

        return stops
