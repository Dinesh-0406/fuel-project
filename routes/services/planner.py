"""Orchestrates a complete route + fuel plan request.

This is the single place the view calls. It owns the ordering of the work and
the plan-level cache, and it is where the "how many external calls did we make"
question is answered:

    geocoding : 0 (cached) or 1-2 (uncached)
    routing   : 0 (cached) or 1
    stations  : 0 -- always local computation
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from django.conf import settings
from django.core.cache import cache
from django.db.models import Count, Max

from routes.exceptions import NoFeasibleFuelPlan
from routes.models import FuelStation
from routes.services.fuel_optimizer import FuelPlan, FuelPlanOptimizer, InfeasiblePlan
from routes.services.geocoding import CachedGeocoder, GeocodedLocation, get_geocoding_provider
from routes.services.geometry import RouteGeometry
from routes.services.routing import CachedRoutingProvider, RouteResult, get_routing_provider
from routes.services.station_finder import StationFinder, StationSearchResult

logger = logging.getLogger(__name__)


@dataclass
class PlanMetrics:
    """Instrumentation surfaced in the API's ``meta`` block."""

    geocoding_calls: int = 0
    routing_calls: int = 0
    route_cache_hit: bool = False
    stations_in_bounding_box: int = 0
    stations_considered: int = 0
    route_vertices: int = 0
    routes_considered: int = 1  # alternatives costed, all from one call
    elapsed_ms: float = 0.0  # whole request, including any external calls
    routing_ms: float = 0.0  # time spent waiting on the routing provider
    local_ms: float = 0.0  # geometry + station search + optimisation


@dataclass
class RoutePlan:
    start: GeocodedLocation
    finish: GeocodedLocation
    route: RouteResult
    geometry: RouteGeometry
    fuel_plan: FuelPlan
    metrics: PlanMetrics = field(default_factory=PlanMetrics)
    corridor_miles: float = 0.0


class RoutePlanner:
    """Builds a route and its cost-optimal fuel plan."""

    def __init__(
        self,
        geocoder: CachedGeocoder | None = None,
        router: CachedRoutingProvider | None = None,
        station_finder: StationFinder | None = None,
        optimizer: FuelPlanOptimizer | None = None,
    ) -> None:
        self.geocoder = geocoder or get_geocoding_provider()
        self.router = router or get_routing_provider()
        self.station_finder = station_finder or StationFinder()
        self.optimizer = optimizer or FuelPlanOptimizer(
            max_range_miles=settings.VEHICLE_MAX_RANGE_MILES,
            mpg=settings.VEHICLE_MPG,
            reserve_gallons=settings.FUEL_RESERVE_GALLONS,
        )

    def resolve_endpoints(
        self, start_query: str, finish_query: str
    ) -> tuple[GeocodedLocation, GeocodedLocation, int]:
        """Geocode both endpoints, returning them plus the upstream call count.

        Split out from :meth:`plan` so the caller can build a plan cache key from
        the resolved coordinates and skip the remaining work entirely on a hit.
        """
        before = self.geocoder.call_count
        start = self.geocoder.geocode(start_query)
        finish = self.geocoder.geocode(finish_query)
        return start, finish, self.geocoder.call_count - before

    def plan(self, start_query: str, finish_query: str) -> RoutePlan:
        start, finish, geocoding_calls = self.resolve_endpoints(start_query, finish_query)
        return self.plan_for_endpoints(start, finish, geocoding_calls=geocoding_calls)

    def plan_for_endpoints(
        self,
        start: GeocodedLocation,
        finish: GeocodedLocation,
        *,
        geocoding_calls: int = 0,
    ) -> RoutePlan:
        started = time.perf_counter()
        metrics = PlanMetrics(geocoding_calls=geocoding_calls)
        start_query, finish_query = start.query, finish.query

        # One routing call per uncached origin/destination pair, which may carry
        # several alternatives back with it.
        routing_started = time.perf_counter()
        options = self.router.routes(start.as_tuple, finish.as_tuple)
        metrics.routing_ms = (time.perf_counter() - routing_started) * 1000.0
        metrics.route_cache_hit = bool(self.router.last_call_was_cached)
        metrics.routing_calls = 0 if metrics.route_cache_hit else 1
        metrics.routes_considered = len(options)

        # The shortest road is not always the cheapest to drive: fuel prices vary
        # by region, so every option the provider returned is costed in full and
        # the cheapest wins. This is local work -- no extra provider calls.
        best: tuple[FuelPlan, RouteResult, RouteGeometry, StationSearchResult] | None = None
        failure: InfeasiblePlan | None = None

        for option in options:
            geometry = RouteGeometry(
                option.coordinates,
                option.distance_miles,
                resample_miles=settings.ROUTE_RESAMPLE_MILES,
                grid_cell_miles=max(25.0, self.station_finder.corridor_miles * 2.0),
            )
            search = self.station_finder.find(geometry)
            try:
                candidate_plan = self.optimizer.plan(search.candidates, option.distance_miles)
            except InfeasiblePlan as exc:
                failure = failure or exc
                continue
            if best is None or candidate_plan.total_cost < best[0].total_cost:
                best = (candidate_plan, option, geometry, search)

        if best is None:
            exc = failure or InfeasiblePlan("No route could be fuelled to the destination.")
            logger.warning("No feasible fuel plan for %r -> %r: %s", start_query, finish_query, exc)
            raise NoFeasibleFuelPlan(str(exc), details=exc.details) from exc

        fuel_plan, route, geometry, search = best
        metrics.route_vertices = route.vertex_count
        metrics.stations_in_bounding_box = search.stations_in_bounding_box
        metrics.stations_considered = len(search.candidates)

        metrics.elapsed_ms = (time.perf_counter() - started) * 1000.0
        metrics.local_ms = metrics.elapsed_ms - metrics.routing_ms
        logger.info(
            "Planned %r -> %r: %.1f mi, %d stop(s), $%s, %d geocoding call(s), "
            "%d routing call(s), %.0fms total (%.0fms local)",
            start_query,
            finish_query,
            route.distance_miles,
            len(fuel_plan.stops),
            fuel_plan.total_cost,
            metrics.geocoding_calls,
            metrics.routing_calls,
            metrics.elapsed_ms,
            metrics.local_ms,
        )
        return RoutePlan(
            start=start,
            finish=finish,
            route=route,
            geometry=geometry,
            fuel_plan=fuel_plan,
            metrics=metrics,
            corridor_miles=search.corridor_miles,
        )


def dataset_version() -> str:
    """Cheap fingerprint of the station table, used in plan cache keys.

    Changes whenever stations are added, removed or re-enriched, so re-importing
    the dataset transparently invalidates every cached plan.
    """
    key = "dataset:version:v1"
    cached = cache.get(key)
    if cached is not None:
        return cached

    aggregate = FuelStation.objects.geocoded().aggregate(
        count=Count("id"), latest=Max("updated_at")
    )
    latest = aggregate["latest"]
    version = f"{aggregate['count']}:{latest.isoformat() if latest else 'none'}"
    cache.set(key, version, 300)
    return version
