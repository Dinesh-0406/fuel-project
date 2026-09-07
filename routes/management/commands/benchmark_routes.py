"""Measure uncached vs cached planning latency and the external-call budget.

Run against the live providers:

    python manage.py benchmark_routes

or entirely offline, which is what CI does:

    python manage.py benchmark_routes --offline
"""

from __future__ import annotations

import contextlib
import time
from statistics import mean
from unittest import mock

from django.core.cache import cache
from django.core.management.base import BaseCommand

from routes.services.planner import RoutePlanner

DEFAULT_ROUTES = [
    ("New York, NY", "Chicago, IL"),
    ("Los Angeles, CA", "Denver, CO"),
    ("Seattle, WA", "Miami, FL"),
]


class _CallCounter:
    """Wraps requests.get to count how many external calls a plan really makes."""

    def __init__(self):
        self.count = 0
        self._real = None

    def __enter__(self):
        import requests

        self._real = requests.get

        def counted(*args, **kwargs):
            self.count += 1
            return self._real(*args, **kwargs)

        self._patch = mock.patch("routes.services.http.requests.get", side_effect=counted)
        self._patch.start()
        return self

    def __exit__(self, *exc):
        self._patch.stop()
        return False


class Command(BaseCommand):
    help = "Benchmark uncached vs cached route planning."

    def add_arguments(self, parser):
        parser.add_argument(
            "--start", type=str, default="", help="Start location (single-route mode)."
        )
        parser.add_argument(
            "--finish", type=str, default="", help="Finish location (single-route mode)."
        )
        parser.add_argument("--repeats", type=int, default=3, help="Cached measurements per route.")
        parser.add_argument(
            "--offline",
            action="store_true",
            help="Use a synthetic route instead of calling live providers.",
        )

    def handle(self, *args, **options):
        if options["offline"]:
            self._run_offline(options["repeats"])
            return

        pairs = (
            [(options["start"], options["finish"])]
            if options["start"] and options["finish"]
            else DEFAULT_ROUTES
        )

        self.stdout.write(
            self.style.MIGRATE_HEADING(
                f"{'Route':<34}{'uncached':>11}{'cached':>10}{'calls':>8}{'stops':>7}"
            )
        )

        for start, finish in pairs:
            cache.clear()
            planner = RoutePlanner()

            with _CallCounter() as counter:
                begin = time.perf_counter()
                plan = planner.plan(start, finish)
                uncached_ms = (time.perf_counter() - begin) * 1000.0
                external_calls = counter.count

            cached_times = []
            for _ in range(options["repeats"]):
                begin = time.perf_counter()
                RoutePlanner().plan(start, finish)
                cached_times.append((time.perf_counter() - begin) * 1000.0)

            label = f"{start} -> {finish}"
            self.stdout.write(
                f"{label:<34}{uncached_ms:>9.0f}ms{mean(cached_times):>8.0f}ms"
                f"{external_calls:>8}{len(plan.fuel_plan.stops):>7}"
            )
            self.stdout.write(
                self.style.HTTP_INFO(
                    f"  {plan.route.distance_miles:.0f} mi, "
                    f"{plan.metrics.stations_considered} station(s) in corridor, "
                    f"total ${plan.fuel_plan.total_cost}"
                )
            )

        self.stdout.write(
            "\nThe external-call count is the headline number: it stays constant "
            "no matter how many stations lie along the route."
        )

    def _run_offline(self, repeats: int) -> None:
        """Time only the local work, with routing and geocoding stubbed out."""
        from routes.services.fuel_optimizer import FuelPlanOptimizer, InfeasiblePlan
        from routes.services.geometry import RouteGeometry
        from routes.services.station_finder import StationFinder

        coordinates = [
            (-74.0 - (13.6 * i / 4000.0), 40.71 + (1.17 * i / 4000.0)) for i in range(4000)
        ]

        timings: dict[str, list[float]] = {"geometry": [], "stations": [], "optimizer": []}
        for _ in range(max(1, repeats)):
            begin = time.perf_counter()
            geometry = RouteGeometry(coordinates, 790.0)
            timings["geometry"].append((time.perf_counter() - begin) * 1000.0)

            begin = time.perf_counter()
            found = StationFinder().find(geometry)
            timings["stations"].append((time.perf_counter() - begin) * 1000.0)

            begin = time.perf_counter()
            # InfeasiblePlan is expected when the local dataset has no stations
            # along this synthetic corridor; we are timing the attempt either way.
            with contextlib.suppress(InfeasiblePlan):
                FuelPlanOptimizer(500.0, 10.0).plan(found.candidates, 790.0)
            timings["optimizer"].append((time.perf_counter() - begin) * 1000.0)

        self.stdout.write(self.style.MIGRATE_HEADING("Local computation (no network)"))
        for label, values in timings.items():
            self.stdout.write(f"  {label:<12} {mean(values):>8.1f} ms")
        self.stdout.write(f"  {'candidates':<12} {len(found.candidates):>8}")
