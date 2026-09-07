"""Cost-optimal fuel stop selection along a route.

The problem
-----------
Stations sit at known distances along the route, each with a price per gallon.
The vehicle starts with a full tank, burns fuel at a fixed MPG and can never
hold more than ``max_range_miles / mpg`` gallons. Choose where to stop and how
many gallons to buy at each stop so that the destination is reached and the
total spend is minimised.

This is the classic *gas station problem*. The optimal strategy is a greedy one:

    At the current stop, look ahead as far as a full tank can carry you.
      * If any reachable station is cheaper, buy just enough fuel to reach the
        FIRST such station -- there is no reason to buy expensive fuel now when
        cheaper fuel is reachable.
      * Otherwise this is the cheapest fuel in reach, so fill the tank
        completely and continue to the CHEAPEST reachable station.
      * If the destination itself is reachable, buy only enough to arrive and
        stop buying.

The start is treated as a stop where fuel is already owned (no purchase
possible), so the first move is simply "drive to the cheapest station in range".

Both look-ups run in O(1) after an O(n log n) preprocessing pass:
``_next_cheaper`` (monotonic stack) answers "first cheaper station ahead" and a
sparse table answers "cheapest station in a distance window". The whole planner
is therefore O(n log n) and does no I/O.

The greedy's optimality is not taken on faith: ``tests/test_optimizer.py``
compares it against an exhaustive dynamic-programming oracle over thousands of
randomised instances.
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


class _RangeMinPrice:
    """Sparse table over station prices.

    ``query(lo, hi)`` returns the index of the cheapest station in ``[lo, hi]``,
    breaking ties towards the FURTHEST station so the vehicle makes as much
    progress as possible on a tank of equally-cheap fuel.
    """

    def __init__(self, prices: Sequence[Decimal]) -> None:
        self._prices = prices
        n = len(prices)
        self._log = [0] * (n + 1)
        for i in range(2, n + 1):
            self._log[i] = self._log[i // 2] + 1
        levels = self._log[n] + 1 if n else 1
        self._table: list[list[int]] = [list(range(n))]
        for k in range(1, levels):
            span = 1 << k
            prev = self._table[k - 1]
            row = [0] * max(0, n - span + 1)
            for i in range(len(row)):
                row[i] = self._better(prev[i], prev[i + (span >> 1)])
            self._table.append(row)

    def _better(self, a: int, b: int) -> int:
        if self._prices[b] < self._prices[a]:
            return b
        if self._prices[b] == self._prices[a]:
            return max(a, b)
        return a

    def query(self, lo: int, hi: int) -> int:
        k = self._log[hi - lo + 1]
        return self._better(self._table[k][lo], self._table[k][hi - (1 << k) + 1])


def _next_cheaper(prices: Sequence[Decimal]) -> list[int]:
    """For each index, the next index with a strictly lower price (-1 if none)."""
    result = [-1] * len(prices)
    stack: list[int] = []
    for i, price in enumerate(prices):
        while stack and prices[stack[-1]] > price:
            result[stack.pop()] = i
        stack.append(i)
    return result


class FuelPlanOptimizer:
    """Selects cost-optimal fuel stops for a fixed vehicle configuration."""

    def __init__(
        self,
        max_range_miles: float,
        mpg: float,
        *,
        reserve_gallons: float = 0.0,
    ) -> None:
        if max_range_miles <= 0 or mpg <= 0:
            raise ValueError("max_range_miles and mpg must be positive.")
        self.max_range_miles = float(max_range_miles)
        self.mpg = float(mpg)
        self.tank_capacity_gallons = self.max_range_miles / self.mpg
        self.reserve_gallons = max(0.0, float(reserve_gallons))
        # Usable range after holding back the safety reserve.
        self.usable_range_miles = max(
            0.0, (self.tank_capacity_gallons - self.reserve_gallons) * self.mpg
        )

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

        stops = self._greedy(stations, destination, start_fuel)

        total_cost = sum((s.cost for s in stops), Decimal("0.00"))
        total_gallons = sum(s.fuel_purchased_gallons for s in stops)
        consumed = destination / self.mpg
        remaining = start_fuel + total_gallons - consumed

        logger.info(
            "Fuel plan: %d stop(s), %.3f gal purchased, $%s total over %.1f miles",
            len(stops),
            total_gallons,
            total_cost,
            destination,
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
            if (
                deduped
                and abs(station.distance_along_route_miles - deduped[-1].distance_along_route_miles)
                < EPSILON_MILES
            ):
                # Same point on the route: keep whichever is cheaper (already
                # first thanks to the sort key).
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
        reachable = start_fuel * self.mpg
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

    def _greedy(
        self, stations: Sequence[CandidateStation], destination: float, start_fuel: float
    ) -> list[FuelStop]:
        if not stations or destination <= start_fuel * self.mpg + EPSILON_MILES:
            return []

        positions = [s.distance_along_route_miles for s in stations]
        prices = [s.price for s in stations]
        next_cheaper = _next_cheaper(prices)
        range_index = _RangeMinPrice(prices)
        n = len(stations)

        def last_within(from_mile: float, reach_miles: float) -> int:
            """Index of the furthest station within ``reach_miles`` of ``from_mile``."""
            limit = from_mile + reach_miles + EPSILON_MILES
            lo, hi, best = 0, n - 1, -1
            while lo <= hi:
                mid = (lo + hi) // 2
                if positions[mid] <= limit:
                    best, lo = mid, mid + 1
                else:
                    hi = mid - 1
            return best

        stops: list[FuelStop] = []
        position = 0.0
        fuel = start_fuel
        previous_stop_mile = 0.0

        # First move: we cannot buy at the start, so drive to the cheapest
        # station within the range of the fuel already in the tank.
        furthest = last_within(position, fuel * self.mpg)
        first_candidate = 0
        if furthest < first_candidate:
            raise InfeasiblePlan("No fuel station is reachable from the start location.")
        index = range_index.query(first_candidate, furthest)

        while True:
            station = stations[index]
            travelled = station.distance_along_route_miles - position
            fuel_before = fuel - travelled / self.mpg
            # Guard against accumulated float drift producing a tiny negative.
            if fuel_before < -1e-6:
                raise InfeasiblePlan("Vehicle ran out of fuel before reaching a station.")
            fuel_before = max(0.0, fuel_before)
            position = station.distance_along_route_miles

            remaining_to_destination = destination - position
            reach_full_tank = self.usable_range_miles

            # The destination behaves like a node whose fuel is free: it is
            # "cheaper" than every station. So the look-ahead targets whichever
            # comes first -- a strictly cheaper station, or the destination.
            cheaper = next_cheaper[index]
            cheaper_is_useful = (
                cheaper != -1
                and positions[cheaper] - position <= reach_full_tank + EPSILON_MILES
                and positions[cheaper] < destination - EPSILON_MILES
            )

            if cheaper_is_useful:
                # Cheaper fuel is reachable: buy only enough to get there.
                target_miles = positions[cheaper] - position
                next_index = cheaper
            elif remaining_to_destination <= reach_full_tank + EPSILON_MILES:
                # Nothing cheaper in between: buy just enough to finish the trip.
                target_miles = remaining_to_destination
                next_index = None
            else:
                # Nothing cheaper in reach and the destination is too far: this is
                # the cheapest fuel available for a while, so fill the tank and
                # continue to the cheapest station still reachable.
                furthest = last_within(position, reach_full_tank)
                if furthest <= index:
                    raise InfeasiblePlan(
                        "No onward fuel station is reachable from "
                        f"{station.city}, {station.state}."
                    )
                next_index = range_index.query(index + 1, furthest)
                target_miles = math.inf  # fill completely

            needed_gallons = (
                self.tank_capacity_gallons if math.isinf(target_miles) else target_miles / self.mpg
            )
            purchase = min(
                max(0.0, needed_gallons - fuel_before),
                self.tank_capacity_gallons - fuel_before,
            )
            fuel_after = fuel_before + purchase

            if purchase > 0.0:
                cost = (Decimal(repr(purchase)) * station.price).quantize(
                    CENTS, rounding=ROUND_HALF_UP
                )
                stops.append(
                    FuelStop(
                        sequence=len(stops) + 1,
                        station=station,
                        price_per_gallon=station.price,
                        distance_from_start_miles=position,
                        distance_from_previous_stop_miles=position - previous_stop_mile,
                        distance_to_destination_miles=remaining_to_destination,
                        fuel_before_purchase_gallons=fuel_before,
                        fuel_purchased_gallons=purchase,
                        fuel_after_purchase_gallons=fuel_after,
                        cost=cost,
                    )
                )
                previous_stop_mile = position

            fuel = fuel_after
            if next_index is None:
                break
            index = next_index

        return stops
