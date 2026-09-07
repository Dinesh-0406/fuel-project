"""API views. Deliberately thin -- all behaviour lives in ``routes.services``."""

from __future__ import annotations

import copy
import logging
import time

from django.conf import settings
from django.core.cache import cache
from django.shortcuts import render
from drf_spectacular.utils import OpenApiExample, extend_schema
from rest_framework import status
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from routes.serializers import RoutePlanResponseBuilder, RouteRequestSerializer
from routes.services.cache import plan_cache_key
from routes.services.planner import RoutePlanner, dataset_version

logger = logging.getLogger(__name__)


class RoutePlanView(APIView):
    """Plan a US driving route and its cost-optimal fuel stops."""

    http_method_names = ["post", "options"]

    @extend_schema(
        request=RouteRequestSerializer,
        responses={200: dict},
        summary="Plan a route with cost-optimal fuel stops",
        examples=[
            OpenApiExample(
                "Long trip requiring multiple stops",
                value={"start": "New York, NY", "finish": "Los Angeles, CA"},
                request_only=True,
            )
        ],
    )
    def post(self, request: Request) -> Response:
        serializer = RouteRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        start = serializer.validated_data["start"]
        finish = serializer.validated_data["finish"]

        logger.info("Route request: %r -> %r", start, finish)

        started = time.perf_counter()
        planner = RoutePlanner()

        # Geocode first (cheap and cached), then look for a ready-made plan
        # keyed by the RESOLVED coordinates. A hit skips routing, station search
        # and optimisation altogether, so differently-spelled requests for the
        # same journey share one cached plan.
        start_location, finish_location, geocoding_calls = planner.resolve_endpoints(start, finish)
        key = plan_cache_key(
            start_location.as_tuple,
            finish_location.as_tuple,
            corridor_miles=planner.station_finder.corridor_miles,
            max_range_miles=settings.VEHICLE_MAX_RANGE_MILES,
            mpg=settings.VEHICLE_MPG,
            dataset_version=dataset_version(),
        )
        cached = cache.get(key)
        if cached is not None:
            payload = copy.deepcopy(cached)
            payload["meta"]["plan_cache_hit"] = True
            payload["meta"]["external_calls"]["geocoding"] = geocoding_calls
            payload["meta"]["external_calls"]["routing"] = 0
            # Report this request's own timing, not the timing of the request
            # that originally populated the cache.
            elapsed_ms = round((time.perf_counter() - started) * 1000.0, 1)
            payload["meta"]["timing_ms"] = {
                "total": elapsed_ms,
                "routing_provider": 0.0,
                "local": elapsed_ms,
            }
            # Echo the caller's own spelling back rather than the cached one.
            payload["start"]["input"] = start
            payload["finish"]["input"] = finish
            logger.info("Plan cache hit for %r -> %r", start, finish)
            return Response(payload, status=status.HTTP_200_OK)

        plan = planner.plan_for_endpoints(
            start_location, finish_location, geocoding_calls=geocoding_calls
        )
        payload = RoutePlanResponseBuilder.build(plan)
        payload["meta"]["plan_cache_hit"] = False
        cache.set(key, payload, settings.PLAN_CACHE_TTL_SECONDS)
        return Response(payload, status=status.HTTP_200_OK)


def map_demo(request):
    """Minimal Leaflet page for visualising a planned route."""
    return render(request, "routes/map.html")
