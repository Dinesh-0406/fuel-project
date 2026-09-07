"""Fuel optimisation tests.

Two layers of assurance:

1. Scenario tests (A-N) covering each situation called out in the assignment.
2. A property test comparing the greedy against an exhaustive dynamic-programming
   oracle over thousands of randomised instances. The oracle makes no assumptions
   about the shape of the optimal solution -- it enumerates every purchase
   quantity -- so agreement is strong evidence the greedy really is optimal.
"""

from __future__ import annotations

import random
from decimal import Decimal
from functools import cache

import pytest

from routes.services.fuel_optimizer import (
    CandidateStation,
    FuelPlanOptimizer,
    InfeasiblePlan,
)
from routes.tests.conftest import make_candidate

MAX_RANGE = 500.0
MPG = 10.0
CAPACITY = 50.0


@pytest.fixture
def optimizer() -> FuelPlanOptimizer:
    return FuelPlanOptimizer(MAX_RANGE, MPG)


def plan_for(optimizer, stations, distance, **kwargs):
    return optimizer.plan(stations, distance, **kwargs)


def assert_plan_is_feasible(plan, distance: float, capacity: float = CAPACITY):
    """Re-simulate the plan and assert the vehicle never runs dry or overfills."""
    fuel = plan.starting_fuel_gallons
    position = 0.0
    for stop in plan.stops:
        travelled = stop.distance_from_start_miles - position
        fuel -= travelled / MPG
        assert fuel >= -1e-6, f"ran out of fuel before mile {stop.distance_from_start_miles}"
        assert abs(fuel - stop.fuel_before_purchase_gallons) < 1e-6
        fuel += stop.fuel_purchased_gallons
        assert fuel <= capacity + 1e-6, "tank overfilled"
        assert abs(fuel - stop.fuel_after_purchase_gallons) < 1e-6
        position = stop.distance_from_start_miles
    fuel -= (distance - position) / MPG
    assert fuel >= -1e-6, "ran out of fuel before the destination"


# ---------------------------------------------------------------------------
# Scenario coverage
# ---------------------------------------------------------------------------


def test_a_short_trip_under_range_needs_no_stops(optimizer):
    """A: a full tank already covers the trip, so nothing is bought."""
    plan = plan_for(optimizer, [make_candidate(100, "3.00")], 400.0)
    assert plan.stops == []
    assert plan.total_cost == Decimal("0.00")
    assert plan.total_gallons_purchased == 0.0


def test_b_trip_longer_than_range_requires_stops(optimizer):
    """B: beyond 500 miles at least one purchase is unavoidable."""
    stations = [make_candidate(p, "3.00") for p in (200, 450, 700)]
    plan = plan_for(optimizer, stations, 900.0)
    assert len(plan.stops) >= 1
    assert plan.total_cost > Decimal("0.00")
    assert_plan_is_feasible(plan, 900.0)


def test_c_expensive_then_cheap_buys_only_enough_to_reach_the_cheap_station(optimizer):
    """C: at a dear station, buy just enough to reach the cheaper one ahead.

    The $3.00 station at mile 850 is out of reach on the starting tank, so the
    $4.00 station at mile 400 cannot be skipped. The plan must still refuse to
    fill up there: it buys exactly the 45 gallons needed for the 450-mile leg.
    """
    stations = [make_candidate(400, "4.00"), make_candidate(850, "3.00")]
    plan = plan_for(optimizer, stations, 1200.0)

    first = plan.stops[0]
    assert first.price_per_gallon == Decimal("4.00")
    # Arrives with 10 gal; the leg to the cheaper station needs 45 gal.
    assert first.fuel_before_purchase_gallons == pytest.approx(10.0, abs=1e-6)
    assert first.fuel_purchased_gallons == pytest.approx(35.0, abs=1e-6)
    assert first.fuel_after_purchase_gallons == pytest.approx(45.0, abs=1e-6)
    assert first.fuel_after_purchase_gallons < CAPACITY  # deliberately not filled

    assert plan.stops[1].price_per_gallon == Decimal("3.00")
    assert_plan_is_feasible(plan, 1200.0)


def test_c_expensive_station_is_skipped_when_the_cheap_one_is_reachable(optimizer):
    """The mirror case: never stop at dear fuel you can simply drive past."""
    stations = [make_candidate(400, "4.00"), make_candidate(500, "3.00")]
    plan = plan_for(optimizer, stations, 900.0)

    assert [s.distance_from_start_miles for s in plan.stops] == [500.0]
    assert plan.total_cost == Decimal("120.00")  # 40 gal @ $3.00


def test_d_cheap_then_expensive_fills_up_at_the_cheap_station(optimizer):
    """D: no cheaper fuel ahead, so fill the tank at the cheap station."""
    stations = [make_candidate(400, "3.00"), make_candidate(800, "4.00")]
    plan = plan_for(optimizer, stations, 1200.0)

    first = plan.stops[0]
    assert first.price_per_gallon == Decimal("3.00")
    assert first.fuel_after_purchase_gallons == pytest.approx(CAPACITY, abs=1e-6)
    assert_plan_is_feasible(plan, 1200.0)


def test_e_multiple_cheap_stations_prefers_the_cheapest_reachable(optimizer):
    """E: among several options the plan concentrates spend on the cheapest."""
    stations = [
        make_candidate(100, "4.00"),
        make_candidate(200, "2.50"),
        make_candidate(300, "3.90"),
        make_candidate(700, "3.80"),
    ]
    plan = plan_for(optimizer, stations, 1100.0)
    cheapest_stop = min(plan.stops, key=lambda s: s.price_per_gallon)
    assert cheapest_stop.price_per_gallon == Decimal("2.50")
    # The cheapest station is filled to the brim.
    assert cheapest_stop.fuel_after_purchase_gallons == pytest.approx(CAPACITY, abs=1e-6)
    assert_plan_is_feasible(plan, 1100.0)


def test_f_no_station_within_range_is_infeasible(optimizer):
    """F: a 600-mile gap with no station in it cannot be bridged."""
    with pytest.raises(InfeasiblePlan):
        plan_for(optimizer, [make_candidate(550, "3.00")], 900.0)


def test_f_no_stations_at_all_is_infeasible(optimizer):
    with pytest.raises(InfeasiblePlan):
        plan_for(optimizer, [], 900.0)


def test_g_destination_within_remaining_range_stops_buying(optimizer):
    """G: the last purchase covers exactly the remaining distance, no more."""
    stations = [make_candidate(450, "3.00")]
    plan = plan_for(optimizer, stations, 800.0)
    stop = plan.stops[-1]
    # 350 miles remain -> 35 gallons; 5 remain in the tank -> buy 30.
    assert stop.fuel_purchased_gallons == pytest.approx(30.0, abs=1e-6)
    assert plan.fuel_remaining_at_destination_gallons == pytest.approx(0.0, abs=1e-6)


def test_h_station_slightly_off_route_is_still_usable(optimizer):
    """H: corridor offset does not disqualify a station from the plan."""
    stations = [make_candidate(450, "2.00", offset_miles=8.5)]
    plan = plan_for(optimizer, stations, 800.0)
    assert plan.stops[0].station.offset_from_route_miles == 8.5


def test_i_station_near_the_start_is_skipped_when_not_worthwhile(optimizer):
    """I: an expensive station 5 miles in is ignored -- the tank is already full."""
    stations = [make_candidate(5, "4.50"), make_candidate(450, "3.00")]
    plan = plan_for(optimizer, stations, 800.0)
    assert [s.distance_from_start_miles for s in plan.stops] == [450.0]


def test_j_station_near_the_destination_is_used_when_needed(optimizer):
    stations = [make_candidate(480, "3.00"), make_candidate(950, "2.80")]
    plan = plan_for(optimizer, stations, 1000.0)
    assert 950.0 in [s.distance_from_start_miles for s in plan.stops]
    assert_plan_is_feasible(plan, 1000.0)


def test_k_duplicate_stations_at_one_point_keep_only_the_cheapest(optimizer):
    """K: two stations at the same milepost -- the dearer one is never chosen."""
    stations = [
        make_candidate(450, "4.00", station_id=1),
        make_candidate(450, "3.00", station_id=2),
    ]
    plan = plan_for(optimizer, stations, 800.0)
    assert len(plan.stops) == 1
    assert plan.stops[0].station.station_id == 2


def test_l_multiple_stations_in_one_city_are_all_considered(optimizer):
    stations = [
        make_candidate(440, "3.60", station_id=1, city="Springfield"),
        make_candidate(445, "3.10", station_id=2, city="Springfield"),
        make_candidate(450, "3.40", station_id=3, city="Springfield"),
    ]
    plan = plan_for(optimizer, stations, 800.0)
    assert plan.stops[0].station.station_id == 2


def test_m_exactly_max_range_between_stations_is_feasible(optimizer):
    """M: a gap of exactly 500 miles must not be rejected by float rounding."""
    stations = [make_candidate(500, "3.00"), make_candidate(1000, "3.00")]
    plan = plan_for(optimizer, stations, 1400.0)
    assert len(plan.stops) == 2
    assert_plan_is_feasible(plan, 1400.0)


def test_n_slightly_more_than_max_range_is_infeasible(optimizer):
    """N: 500.5 miles between stations is beyond the vehicle."""
    stations = [make_candidate(500, "3.00"), make_candidate(1000.5, "3.00")]
    with pytest.raises(InfeasiblePlan):
        plan_for(optimizer, stations, 1400.0)


# ---------------------------------------------------------------------------
# Vehicle constraint and money handling
# ---------------------------------------------------------------------------


def test_tank_capacity_is_never_exceeded(optimizer):
    stations = [make_candidate(p, "3.00") for p in range(100, 2000, 100)]
    plan = plan_for(optimizer, stations, 2000.0)
    for stop in plan.stops:
        assert stop.fuel_after_purchase_gallons <= CAPACITY + 1e-9
    assert_plan_is_feasible(plan, 2000.0)


def test_no_leg_between_stops_exceeds_max_range(optimizer):
    stations = [make_candidate(p, "3.00") for p in (300, 700, 1100, 1500)]
    plan = plan_for(optimizer, stations, 1900.0)
    positions = [0.0] + [s.distance_from_start_miles for s in plan.stops] + [1900.0]
    for previous, current in zip(positions, positions[1:], strict=False):
        assert current - previous <= MAX_RANGE + 1e-6


def test_fuel_consumption_follows_ten_mpg(optimizer):
    plan = plan_for(optimizer, [make_candidate(400, "3.00")], 750.0)
    assert plan.total_fuel_consumed_gallons == pytest.approx(75.0)


def test_costs_are_decimal_and_totals_match_line_items(optimizer):
    stations = [make_candidate(p, "3.259") for p in (400, 850, 1300)]
    plan = plan_for(optimizer, stations, 1700.0)
    assert isinstance(plan.total_cost, Decimal)
    assert plan.total_cost == sum((s.cost for s in plan.stops), Decimal("0.00"))
    for stop in plan.stops:
        assert stop.cost == stop.cost.quantize(Decimal("0.01"))


def test_partial_starting_fuel_is_respected(optimizer):
    """A half tank only reaches 250 miles, so an early stop becomes necessary."""
    stations = [make_candidate(200, "3.00"), make_candidate(600, "3.00")]
    plan = plan_for(optimizer, stations, 900.0, starting_fuel_gallons=25.0)
    assert plan.starting_fuel_gallons == 25.0
    assert plan.stops[0].distance_from_start_miles == 200.0
    assert_plan_is_feasible(plan, 900.0)


def test_stations_beyond_the_destination_are_ignored(optimizer):
    stations = [make_candidate(450, "3.00"), make_candidate(900, "1.00")]
    plan = plan_for(optimizer, stations, 800.0)
    assert all(s.distance_from_start_miles <= 800.0 for s in plan.stops)


# ---------------------------------------------------------------------------
# Property test against an exhaustive oracle
# ---------------------------------------------------------------------------


def _brute_force_minimum_cost(
    positions: tuple[int, ...], prices: tuple[Decimal, ...], distance: int
) -> Decimal | None:
    """Exact minimum cost by exhaustive dynamic programming.

    Instances are constructed so that every quantity is exactly representable:
    positions are multiples of 10 miles and MPG is 10, so one gallon is exactly
    10 miles and fuel can be enumerated in whole gallons with no rounding. Every
    purchase quantity from 0 to a full tank is tried at every station -- the
    search assumes nothing about the structure of the optimal answer.
    """
    capacity = int(CAPACITY)
    count = len(positions)

    @cache
    def best_from(index: int, gallons: int) -> Decimal | None:
        best: Decimal | None = None
        position = positions[index]
        for purchase in range(0, capacity - gallons + 1):
            held = gallons + purchase
            cost = Decimal(purchase) * prices[index]
            if distance - position <= held * 10:
                best = cost if best is None else min(best, cost)
            for onward in range(index + 1, count):
                leg = positions[onward] - position
                if leg <= held * 10:
                    remainder = best_from(onward, held - leg // 10)
                    if remainder is not None:
                        total = cost + remainder
                        best = total if best is None else min(best, total)
        return best

    if distance <= capacity * 10:
        return Decimal("0")

    best: Decimal | None = None
    for index in range(count):
        if positions[index] <= capacity * 10:
            candidate = best_from(index, capacity - positions[index] // 10)
            if candidate is not None:
                best = candidate if best is None else min(best, candidate)
    return best


@pytest.mark.parametrize("seed", [1, 2, 3, 4, 5, 6, 7, 8])
def test_greedy_matches_exhaustive_optimum(optimizer, seed):
    """The greedy must equal the brute-force optimum on random instances."""
    rng = random.Random(seed)
    compared = 0

    for _ in range(120):
        distance = rng.choice([300, 500, 510, 700, 900, 1000, 1200, 1500, 2000])
        slots = list(range(10, distance, 10))
        positions = tuple(sorted(rng.sample(slots, min(rng.randint(1, 8), len(slots)))))
        prices = tuple(
            Decimal(rng.choice([250, 275, 300, 300, 350, 350, 400, 450])) / 100 for _ in positions
        )

        expected = _brute_force_minimum_cost(positions, prices, distance)
        stations = [
            CandidateStation(
                station_id=i,
                opis_truckstop_id=str(i),
                name=f"S{i}",
                address="",
                city="C",
                state="TX",
                latitude=0.0,
                longitude=0.0,
                price=prices[i],
                distance_along_route_miles=float(positions[i]),
                offset_from_route_miles=0.0,
            )
            for i in range(len(positions))
        ]

        try:
            actual = optimizer.plan(stations, float(distance)).total_cost
        except InfeasiblePlan:
            actual = None

        assert (expected is None) == (
            actual is None
        ), f"feasibility disagreement: {positions} prices={prices} distance={distance}"
        if expected is None:
            continue

        compared += 1
        # Tolerance covers only the optimizer's rounding to whole cents.
        assert abs(actual - expected) <= Decimal("0.02"), (
            f"suboptimal plan: got {actual}, optimum {expected} "
            f"for {positions} prices={prices} distance={distance}"
        )

    assert compared > 20, "too few feasible instances to be meaningful"
