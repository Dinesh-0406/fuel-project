"""Proof-by-exhaustion that the planned fuel cost is the least possible cost.

``test_optimizer.py`` already checks the greedy against a dynamic-programming
oracle, but only over full starting tanks, whole-gallon purchases, zero reserve
and at most eight stations. This module closes those gaps.

The oracle
----------
``minimum_cost`` knows exactly two moves: *buy one unit of fuel here* and *drive
to the next node*. It assumes nothing whatsoever about the structure of an
optimal solution -- not that purchases happen at cheap stations, not that tanks
are filled, not that any station is skipped. Every reachable (node, fuel) state
is evaluated, so its answer is the true minimum by construction.

Working in integer *units* of fuel is what makes that exhaustive search exact:
one unit is ``unit_miles`` of driving, and instances are built so every leg is a
whole number of units. An optimal solution therefore always exists on the unit
grid, and enumerating the grid enumerates the problem.

A safety reserve is handled by a change of variable rather than a special case:
fuel that may never be burned is simply removed from both the tank capacity and
the starting fuel, which leaves an equivalent instance with no reserve. That is
why the oracle itself has no notion of a reserve.

What is compared
----------------
The greedy rounds each stop to whole cents, so the *unrounded* purchase decisions
are what get compared against the optimum; the rounded total is then checked to
be within the rounding error it is allowed to accumulate. Comparing the rounded
figure alone would let a genuinely suboptimal plan hide inside the tolerance.
"""

from __future__ import annotations

import itertools
import random
from collections.abc import Sequence
from decimal import Decimal

import pytest

from routes.services.fuel_optimizer import (
    CandidateStation,
    FuelPlan,
    FuelPlanOptimizer,
    InfeasiblePlan,
)

MPG = 10.0
TANK_GALLONS = 50.0
MAX_RANGE = TANK_GALLONS * MPG  # 500 miles

# Float purchases are converted to Decimal via str(), so the only discrepancy
# against exact arithmetic is the last binary digit of a float.
EXACT = Decimal("0.000001")


# ---------------------------------------------------------------------------
# Exhaustive oracle
# ---------------------------------------------------------------------------


def minimum_cost(
    positions: Sequence[int],
    prices: Sequence[Decimal],
    destination: int,
    *,
    capacity_units: int,
    start_units: int,
    gallons_per_unit: Decimal,
    approach_units: Sequence[int] = (),
) -> Decimal | None:
    """True minimum spend, or None when the destination cannot be reached.

    Positions, ``destination``, ``capacity_units`` and ``start_units`` are all in
    integer fuel units (one unit = one leg-step of ``unit_miles``). Stations must
    already be sorted by position. ``approach_units`` is the one-way distance
    from the route to each station, charged again on the way back.
    """
    approach = list(approach_units) or [0] * len(positions)
    nodes = [0, *positions, destination]
    legs = [nodes[i + 1] - nodes[i] for i in range(len(nodes) - 1)]

    # Cost-to-go from the destination is zero at every fuel level.
    ahead: list[Decimal | None] = [Decimal(0)] * (capacity_units + 1)

    # Walk backwards: node 0 is the start (no purchase possible there),
    # nodes 1..n are stations.
    for node in range(len(legs) - 1, -1, -1):
        leg = legs[node]
        here: list[Decimal | None] = [None] * (capacity_units + 1)

        if node >= 1:
            reach = approach[node - 1]
            # Standing at the pump, having driven `reach` off the route: either
            # buy another unit, or drive back out and on to the next junction.
            leaving = reach + leg
            pump: list[Decimal | None] = [
                ahead[fuel - leaving] if fuel >= leaving else None
                for fuel in range(capacity_units + 1)
            ]
            unit_cost = prices[node - 1] * gallons_per_unit
            for fuel in range(capacity_units - 1, -1, -1):
                above = pump[fuel + 1]
                if above is None:
                    continue
                candidate = unit_cost + above
                if pump[fuel] is None or candidate < pump[fuel]:
                    pump[fuel] = candidate

        for fuel in range(capacity_units + 1):
            best = ahead[fuel - leg] if fuel >= leg else None  # drive past
            if node >= 1 and fuel >= approach[node - 1]:
                stopping = pump[fuel - approach[node - 1]]
                if stopping is not None and (best is None or stopping < best):
                    best = stopping
            here[fuel] = best

        ahead = here

    return ahead[start_units]


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


def build_stations(
    positions_miles: Sequence[float],
    prices: Sequence[Decimal],
    offsets_miles: Sequence[float] = (),
):
    offsets = list(offsets_miles) or [0.0] * len(positions_miles)
    return [
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
            distance_along_route_miles=float(positions_miles[i]),
            offset_from_route_miles=float(offsets[i]),
        )
        for i in range(len(positions_miles))
    ]


def on_grid(value: float, what: str, unit_miles: float) -> int:
    """Return ``value`` as an int, refusing anything off the unit grid."""
    nearest = round(value)
    assert abs(value - nearest) < 1e-9, (
        f"{what} is {value} units, which is off the {unit_miles}-mile grid; "
        "the oracle would be solving a different instance"
    )
    return nearest


def exact_cost(plan: FuelPlan) -> Decimal:
    """Plan cost without the per-stop rounding to cents."""
    return sum(
        (Decimal(str(s.fuel_purchased_gallons)) * s.price_per_gallon for s in plan.stops),
        Decimal(0),
    )


def assert_least_possible_cost(
    positions_units: Sequence[int],
    prices: Sequence[Decimal],
    destination_units: int,
    *,
    unit_miles: float,
    tank_gallons: float = TANK_GALLONS,
    reserve_gallons: float = 0.0,
    start_gallons: float | None = None,
    approach_units_per_station: Sequence[int] = (),
) -> Decimal | None:
    """Run the planner and the oracle on one instance and require agreement.

    Returns the optimum cost, or None when the instance is genuinely infeasible.
    """
    assert start_gallons is None or start_gallons >= reserve_gallons, (
        "instances must start at or above the reserve, otherwise the planner "
        "buys fuel to restore a reserve the oracle's shifted model cannot see"
    )

    usable_gallons = tank_gallons - reserve_gallons
    gallons_per_unit = Decimal(str(unit_miles)) / Decimal(str(MPG))
    held = tank_gallons if start_gallons is None else min(start_gallons, tank_gallons)

    # The exhaustive search is only exact while every quantity lands on the unit
    # grid. Rounding here instead would quietly solve a different problem and
    # then report the planner as optimal against it.
    capacity_units = on_grid(usable_gallons * MPG / unit_miles, "tank capacity", unit_miles)
    start_units = on_grid((held - reserve_gallons) * MPG / unit_miles, "starting fuel", unit_miles)

    approach_units = list(approach_units_per_station) or [0] * len(positions_units)

    optimum = minimum_cost(
        positions_units,
        prices,
        destination_units,
        capacity_units=capacity_units,
        start_units=start_units,
        gallons_per_unit=gallons_per_unit,
        approach_units=approach_units,
    )

    # Planning on the instance's own grid keeps the planner and the oracle
    # solving exactly the same problem, so any disagreement is a real one rather
    # than the planner's default resolution rounding differently.
    optimizer = FuelPlanOptimizer(
        tank_gallons * MPG, MPG, reserve_gallons=reserve_gallons, resolution_miles=unit_miles
    )
    stations = build_stations(
        [p * unit_miles for p in positions_units],
        prices,
        [a * unit_miles for a in approach_units],
    )
    distance = destination_units * unit_miles

    try:
        plan = optimizer.plan(stations, distance, starting_fuel_gallons=start_gallons)
    except InfeasiblePlan:
        plan = None

    context = (
        f"positions={list(positions_units)} prices={[str(p) for p in prices]} "
        f"approach={approach_units} destination={destination_units} "
        f"unit_miles={unit_miles} reserve={reserve_gallons} start={start_gallons}"
    )

    assert (optimum is None) == (plan is None), (
        f"feasibility disagreement (oracle={'infeasible' if optimum is None else optimum}, "
        f"planner={'infeasible' if plan is None else 'planned'}): {context}"
    )
    if optimum is None:
        return None

    actual = exact_cost(plan)
    assert actual >= optimum - EXACT, (
        f"planner beat the true optimum, so the oracle is wrong: "
        f"planner={actual} optimum={optimum}: {context}"
    )
    assert actual <= optimum + EXACT, (
        f"suboptimal plan: planner spends {actual}, optimum is {optimum} "
        f"(excess {actual - optimum}): {context}"
    )

    # The reported figure may only differ from the exact one by the per-stop
    # rounding to cents that the planner is entitled to.
    allowance = Decimal("0.005") * len(plan.stops)
    assert abs(plan.total_cost - actual) <= allowance, (
        f"reported total {plan.total_cost} strays from exact cost {actual} "
        f"by more than {len(plan.stops)} cent-roundings: {context}"
    )
    return optimum


# ---------------------------------------------------------------------------
# Exhaustive sweeps -- every instance in a bounded universe, not a sample
# ---------------------------------------------------------------------------


def sweep(slots, price_choices, destination_units, **kwargs) -> tuple[int, int]:
    """Check every possible instance over ``slots`` and ``price_choices``.

    Each slot independently holds no station or a station at any of the offered
    prices, so the sweep covers ``(1 + len(price_choices)) ** len(slots)``
    instances -- the complete space for that layout.
    """
    feasible = total = 0
    for assignment in itertools.product([None, *price_choices], repeat=len(slots)):
        positions = [slot for slot, price in zip(slots, assignment, strict=True) if price]
        prices = [price for price in assignment if price]
        total += 1
        if assert_least_possible_cost(positions, prices, destination_units, **kwargs) is not None:
            feasible += 1
    return feasible, total


PRICES_3 = (Decimal("2.00"), Decimal("3.00"), Decimal("4.00"))
PRICES_2 = (Decimal("2.50"), Decimal("3.50"))


def test_every_instance_on_a_seven_slot_grid_is_planned_at_minimum_cost():
    """All 16,384 station/price combinations on a 100-mile grid to mile 800."""
    slots = (1, 2, 3, 4, 5, 6, 7)  # units of 100 miles
    feasible, total = sweep(slots, PRICES_3, 8, unit_miles=100.0)
    assert total == 4**7
    assert feasible > total // 2, "sweep degenerated into mostly infeasible instances"


def test_every_instance_on_a_nine_slot_grid_is_planned_at_minimum_cost():
    """A longer 1,000-mile trip needing two stops, swept exhaustively."""
    slots = (1, 2, 3, 4, 5, 6, 7, 8, 9)
    feasible, total = sweep(slots, PRICES_2, 10, unit_miles=100.0)
    assert total == 3**9
    assert feasible > total // 2


def test_every_instance_is_planned_at_minimum_cost_with_a_reserve():
    """The same exhaustive sweep with 10 gallons held back as a reserve."""
    slots = (1, 2, 3, 4, 5, 6)
    feasible, total = sweep(slots, PRICES_3, 7, unit_miles=100.0, reserve_gallons=10.0)
    assert total == 4**6
    assert feasible > 0


def test_every_instance_is_planned_at_minimum_cost_from_a_part_full_tank():
    """The same, starting on 30 of 50 gallons rather than a full tank."""
    slots = (1, 2, 3, 4, 5, 6)
    feasible, total = sweep(slots, PRICES_3, 7, unit_miles=100.0, start_gallons=30.0)
    assert total == 4**6
    assert feasible > 0


def test_every_fine_grained_instance_on_a_five_slot_grid_is_optimal():
    """50-mile spacing, so legs are no longer multiples of the tank range."""
    slots = (2, 4, 5, 7, 9)  # units of 50 miles -> 100, 200, 250, 350, 450
    feasible, total = sweep(slots, PRICES_3, 16, unit_miles=50.0)
    assert total == 4**5
    assert feasible > 0


# ---------------------------------------------------------------------------
# Detours -- the reason price alone is not the objective
# ---------------------------------------------------------------------------


def sweep_with_detours(slots, choices, destination_units, **kwargs) -> tuple[int, int]:
    """Every combination of absent / (price, distance off route) over ``slots``."""
    feasible = total = 0
    for assignment in itertools.product([None, *choices], repeat=len(slots)):
        positions, prices, approach = [], [], []
        for slot, choice in zip(slots, assignment, strict=True):
            if choice is None:
                continue
            price, off = choice
            positions.append(slot)
            prices.append(price)
            approach.append(off)
        total += 1
        result = assert_least_possible_cost(
            positions,
            prices,
            destination_units,
            approach_units_per_station=approach,
            **kwargs,
        )
        if result is not None:
            feasible += 1
    return feasible, total


# One unit is 10 miles here, so an offset of 1 unit is a station 10 miles off
# the route -- the corridor's own width, and a 20-mile round trip.
DETOUR_CHOICES = (
    (Decimal("2.50"), 0),
    (Decimal("2.50"), 1),
    (Decimal("3.50"), 0),
    (Decimal("3.50"), 1),
)


def test_every_instance_with_detours_is_planned_at_minimum_total_cost():
    """All 3,125 combinations of price and off-route distance over five slots."""
    slots = (10, 25, 40, 55, 70)  # units of 10 miles
    feasible, total = sweep_with_detours(slots, DETOUR_CHOICES, 90, unit_miles=10.0)
    assert total == 5**5
    assert feasible > total // 2, "sweep degenerated into mostly infeasible instances"


def test_every_detour_instance_holds_with_a_reserve():
    """The same sweep with 5 gallons held back, over four slots."""
    slots = (10, 25, 40, 55)
    feasible, total = sweep_with_detours(
        slots, DETOUR_CHOICES, 75, unit_miles=10.0, reserve_gallons=5.0
    )
    assert total == 5**4
    assert feasible > 0


@pytest.mark.parametrize("seed", range(4))
def test_random_detour_instances_are_optimal(seed):
    """Mixed offsets across the full corridor width, up to 10 stations."""
    rng = random.Random(3000 + seed)
    price_pool = [Decimal(c) / 100 for c in (219, 265, 299, 301, 345, 389, 420)]
    feasible = 0

    for _ in range(60):
        destination = rng.choice([60, 90, 120, 160])  # units of 10 miles
        count = rng.randint(1, 10)
        positions = sorted(rng.sample(range(1, destination), count))
        prices = [rng.choice(price_pool) for _ in positions]
        approach = [rng.choice([0, 0, 1]) for _ in positions]
        if (
            assert_least_possible_cost(
                positions,
                prices,
                destination,
                unit_miles=10.0,
                approach_units_per_station=approach,
            )
            is not None
        ):
            feasible += 1

    assert feasible > 15


def test_tank_capped_top_up_behind_a_detour_matches_the_optimum():
    """The Grand Junction -> Minot shape, checked against the oracle.

    The cheapest fuel fills the tank, so the marginally cheaper station just
    beyond it can only absorb the fuel burned reaching it -- and it sits off the
    route. Whether that trade is worth making is exactly what the oracle decides.
    """
    for approach in ([0, 0, 0], [0, 1, 0], [1, 1, 1]):
        assert_least_possible_cost(
            [40, 41, 61],
            [Decimal("3.169"), Decimal("3.186"), Decimal("3.227")],
            99,
            unit_miles=10.0,
            approach_units_per_station=approach,
        )


# ---------------------------------------------------------------------------
# Randomised instances at realistic granularity
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("seed", range(6))
def test_random_ten_mile_instances_are_optimal(seed):
    """Up to 14 stations, 10-mile positions, prices repeating to force ties."""
    rng = random.Random(seed)
    price_pool = [Decimal(c) / 100 for c in (199, 250, 275, 300, 300, 325, 350, 350, 425, 499)]
    feasible = 0

    for _ in range(150):
        destination = rng.choice([60, 80, 100, 130, 170, 200, 250])  # units of 10 miles
        count = rng.randint(1, 14)
        slots = range(1, destination)
        positions = sorted(rng.sample(list(slots), min(count, len(list(slots)))))
        prices = [rng.choice(price_pool) for _ in positions]
        if assert_least_possible_cost(positions, prices, destination, unit_miles=10.0) is not None:
            feasible += 1

    assert feasible > 40, "too few feasible instances for the run to mean much"


@pytest.mark.parametrize("seed", range(4))
def test_random_single_mile_instances_with_fractional_prices_are_optimal(seed):
    """1-mile positions and 3-decimal prices, as in the real OPIS dataset."""
    rng = random.Random(1000 + seed)
    feasible = 0

    for _ in range(40):
        destination = rng.choice([700, 900, 1150, 1400])  # units of 1 mile
        count = rng.randint(1, 9)
        positions = sorted(rng.sample(range(1, destination), count))
        prices = [Decimal(rng.randrange(2100, 4600)) / 1000 for _ in positions]
        if assert_least_possible_cost(positions, prices, destination, unit_miles=1.0) is not None:
            feasible += 1

    assert feasible > 10


@pytest.mark.parametrize("seed", range(4))
def test_random_instances_with_reserve_and_partial_tank_are_optimal(seed):
    """Reserve and starting fuel varied together -- the untested combination."""
    rng = random.Random(2000 + seed)
    price_pool = [Decimal(c) / 100 for c in (215, 260, 299, 340, 399, 450)]
    feasible = 0

    for _ in range(60):
        reserve = rng.choice([0.0, 2.0, 5.0, 10.0])
        start = rng.choice([reserve, 20.0, 35.0, TANK_GALLONS])
        start = max(start, reserve)
        destination = rng.choice([60, 90, 120, 160])  # units of 10 miles
        count = rng.randint(1, 10)
        positions = sorted(rng.sample(range(1, destination), count))
        prices = [rng.choice(price_pool) for _ in positions]
        if (
            assert_least_possible_cost(
                positions,
                prices,
                destination,
                unit_miles=10.0,
                reserve_gallons=reserve,
                start_gallons=start,
            )
            is not None
        ):
            feasible += 1

    assert feasible > 15


# ---------------------------------------------------------------------------
# Adversarial price shapes
# ---------------------------------------------------------------------------

# Each entry is a price pattern over stations spaced every 100 miles. These are
# the shapes a greedy look-ahead is most likely to get wrong: prices that fall
# just out of reach, ties that must break towards progress, and single cheap
# outliers that should attract the whole purchase.
PRICE_SHAPES = {
    "strictly_falling": ["4.50", "4.00", "3.50", "3.00", "2.50", "2.00", "1.50"],
    "strictly_rising": ["1.50", "2.00", "2.50", "3.00", "3.50", "4.00", "4.50"],
    "valley": ["4.00", "3.00", "2.00", "1.00", "2.00", "3.00", "4.00"],
    "peak": ["1.00", "2.00", "3.00", "4.00", "3.00", "2.00", "1.00"],
    "all_equal": ["3.00"] * 7,
    "sawtooth": ["2.00", "4.00", "2.00", "4.00", "2.00", "4.00", "2.00"],
    "one_bargain_early": ["1.00", "4.00", "4.00", "4.00", "4.00", "4.00", "4.00"],
    "one_bargain_late": ["4.00", "4.00", "4.00", "4.00", "4.00", "4.00", "1.00"],
    "bargain_just_out_of_reach": ["3.00", "3.00", "3.00", "3.00", "3.00", "1.00", "3.00"],
    "cliff": ["4.00", "4.00", "4.00", "1.00", "1.00", "1.00", "1.00"],
}


@pytest.mark.parametrize("shape", sorted(PRICE_SHAPES))
@pytest.mark.parametrize("destination_units", [8, 10, 12])
def test_adversarial_price_shapes_are_planned_at_minimum_cost(shape, destination_units):
    prices = [Decimal(p) for p in PRICE_SHAPES[shape]]
    positions = list(range(1, len(prices) + 1))  # 100-mile spacing
    assert_least_possible_cost(positions, prices, destination_units, unit_miles=100.0)


@pytest.mark.parametrize("shape", sorted(PRICE_SHAPES))
def test_adversarial_price_shapes_hold_with_reserve_and_partial_tank(shape):
    """One unit is 10 gallons here, so reserve and starting fuel are multiples of it."""
    prices = [Decimal(p) for p in PRICE_SHAPES[shape]]
    positions = list(range(1, len(prices) + 1))
    assert_least_possible_cost(
        positions,
        prices,
        9,
        unit_miles=100.0,
        reserve_gallons=10.0,
        start_gallons=30.0,
    )


# ---------------------------------------------------------------------------
# Boundary geometry
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "positions,prices,destination",
    [
        # Stations exactly one tank apart, the whole way.
        ([5, 10, 15], ["3.00", "2.00", "4.00"], 20),
        # A station at the very start, which cannot help but must not break the plan.
        ([0, 4, 9], ["1.00", "3.00", "3.00"], 13),
        # A station exactly at the destination: buying there is always pointless.
        ([4, 9, 12], ["3.00", "3.00", "1.00"], 12),
        # Two stations at the same milepost, cheap one second.
        ([4, 4, 9], ["4.00", "2.00", "3.00"], 13),
        # Everything clustered at the far end of the first tank.
        ([4, 5, 5, 9, 10], ["3.50", "2.00", "2.00", "4.00", "1.00"], 14),
        # A single station, reachable, that must carry the entire trip.
        ([5], ["3.00"], 10),
        # Dense cheap cluster early, nothing after -- range must be hoarded.
        ([1, 2, 3, 4], ["1.00", "1.10", "1.20", "1.30"], 9),
    ],
)
def test_boundary_geometry_is_planned_at_minimum_cost(positions, prices, destination):
    assert_least_possible_cost(
        positions, [Decimal(p) for p in prices], destination, unit_miles=100.0
    )


def test_planner_and_oracle_agree_that_an_unbridgeable_gap_is_infeasible():
    """Sanity check in the other direction: both must call this impossible."""
    optimum = assert_least_possible_cost(
        [1, 7], [Decimal("2.00"), Decimal("2.00")], 12, unit_miles=100.0
    )
    assert optimum is None
