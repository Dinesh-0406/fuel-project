"""Request validation and response shaping.

All request validation lives here so the view stays thin, and all rounding of
money and physical quantities happens in one place so the API is consistent.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from django.conf import settings
from rest_framework import serializers

from routes.services.cache import normalize_location
from routes.services.planner import RoutePlan

MAX_LOCATION_LENGTH = 200


def _money(value: Decimal) -> str:
    """Serialise money as a fixed 2dp string so no float ever reaches the client."""
    return str(Decimal(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _gallons(value: float) -> float:
    return round(float(value), 3)


def _miles(value: float) -> float:
    return round(float(value), 2)


class RouteRequestSerializer(serializers.Serializer):
    """Validates ``POST /api/v1/routes/``."""

    start = serializers.CharField(
        max_length=MAX_LOCATION_LENGTH,
        trim_whitespace=True,
        allow_blank=False,
        help_text='Human-readable US start location, e.g. "New York, NY".',
    )
    finish = serializers.CharField(
        max_length=MAX_LOCATION_LENGTH,
        trim_whitespace=True,
        allow_blank=False,
        help_text='Human-readable US finish location, e.g. "Chicago, IL".',
    )

    def _clean(self, value: str, field: str) -> str:
        if not normalize_location(value):
            raise serializers.ValidationError(
                {field: ["This field may not contain only punctuation or whitespace."]}
            )
        return value.strip()

    def validate_start(self, value: str) -> str:
        return self._clean(value, "start")

    def validate_finish(self, value: str) -> str:
        return self._clean(value, "finish")

    def validate(self, attrs: dict) -> dict:
        if normalize_location(attrs["start"]) == normalize_location(attrs["finish"]):
            raise serializers.ValidationError(
                {"finish": ["The finish location must differ from the start location."]}
            )
        return attrs


class RoutePlanResponseBuilder:
    """Renders a ``RoutePlan`` into the public JSON structure.

    Kept as a builder rather than a ``ModelSerializer`` because the response is
    assembled from several services rather than one ORM object, and because the
    route geometry can hold tens of thousands of points -- passing it through
    DRF field machinery would be pure overhead.
    """

    @staticmethod
    def build(plan: RoutePlan, *, include_geometry: bool = True) -> dict:
        route = plan.route
        fuel = plan.fuel_plan

        stops = [
            {
                "sequence": stop.sequence,
                "station": {
                    "id": stop.station.station_id,
                    "opis_truckstop_id": stop.station.opis_truckstop_id,
                    "name": stop.station.name,
                    "address": stop.station.address,
                    "city": stop.station.city,
                    "state": stop.station.state,
                    "latitude": stop.station.latitude,
                    "longitude": stop.station.longitude,
                },
                "price_per_gallon": str(
                    stop.price_per_gallon.quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)
                ),
                "distance_from_start_miles": _miles(stop.distance_from_start_miles),
                "distance_from_previous_stop_miles": _miles(stop.distance_from_previous_stop_miles),
                "distance_to_destination_miles": _miles(stop.distance_to_destination_miles),
                "detour_from_route_miles": _miles(stop.station.offset_from_route_miles),
                "fuel_before_purchase_gallons": _gallons(stop.fuel_before_purchase_gallons),
                "fuel_purchased_gallons": _gallons(stop.fuel_purchased_gallons),
                "fuel_after_purchase_gallons": _gallons(stop.fuel_after_purchase_gallons),
                "cost": _money(stop.cost),
            }
            for stop in fuel.stops
        ]

        route_block: dict = {
            "distance_miles": _miles(route.distance_miles),
            "duration_minutes": round(route.duration_minutes, 1),
        }
        if include_geometry:
            route_block["geometry"] = {
                "type": "LineString",
                "coordinates": [[lon, lat] for lon, lat in route.coordinates],
            }

        return {
            "start": {
                "input": plan.start.query,
                "resolved_name": plan.start.display_name,
                "latitude": plan.start.latitude,
                "longitude": plan.start.longitude,
            },
            "finish": {
                "input": plan.finish.query,
                "resolved_name": plan.finish.display_name,
                "latitude": plan.finish.latitude,
                "longitude": plan.finish.longitude,
            },
            "route": route_block,
            "vehicle": {
                "max_range_miles": settings.VEHICLE_MAX_RANGE_MILES,
                "fuel_efficiency_mpg": settings.VEHICLE_MPG,
                "tank_capacity_gallons": round(
                    settings.VEHICLE_MAX_RANGE_MILES / settings.VEHICLE_MPG, 3
                ),
                "starting_fuel_gallons": _gallons(fuel.starting_fuel_gallons),
            },
            "fuel_plan": {
                "stop_count": len(stops),
                "total_gallons_purchased": _gallons(fuel.total_gallons_purchased),
                "total_fuel_consumed_gallons": _gallons(fuel.total_fuel_consumed_gallons),
                "fuel_remaining_at_destination_gallons": _gallons(
                    fuel.fuel_remaining_at_destination_gallons
                ),
                "total_cost": _money(fuel.total_cost),
                "stops": stops,
            },
            "meta": {
                "routing_provider": route.provider,
                "geocoding_provider": "Nominatim",
                "route_corridor_miles": plan.corridor_miles,
                "fuel_station_count_considered": plan.metrics.stations_considered,
                "fuel_stations_in_bounding_box": plan.metrics.stations_in_bounding_box,
                "route_geometry_points": plan.metrics.route_vertices,
                "external_calls": {
                    "geocoding": plan.metrics.geocoding_calls,
                    "routing": plan.metrics.routing_calls,
                },
                "route_cache_hit": plan.metrics.route_cache_hit,
                "timing_ms": {
                    # "local" is the work this service does; "routing" is time
                    # spent waiting on the upstream provider.
                    "total": round(plan.metrics.elapsed_ms, 1),
                    "routing_provider": round(plan.metrics.routing_ms, 1),
                    "local": round(plan.metrics.local_ms, 1),
                },
            },
        }
