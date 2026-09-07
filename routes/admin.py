from __future__ import annotations

from django.contrib import admin
from django.db.models import QuerySet
from django.http import HttpRequest

from routes.models import FuelStation


class GeocodedFilter(admin.SimpleListFilter):
    """Quickly separate stations usable for routing from those that are not."""

    title = "coordinates"
    parameter_name = "geocoded"

    def lookups(self, request, model_admin):
        return [("yes", "Geocoded"), ("no", "Missing coordinates")]

    def queryset(self, request: HttpRequest, queryset: QuerySet) -> QuerySet:
        if self.value() == "yes":
            return queryset.geocoded()
        if self.value() == "no":
            return queryset.filter(latitude__isnull=True) | queryset.filter(longitude__isnull=True)
        return queryset


@admin.register(FuelStation)
class FuelStationAdmin(admin.ModelAdmin):
    list_display = (
        "truckstop_name",
        "city",
        "state",
        "retail_price",
        "latitude",
        "longitude",
        "geocode_source",
        "price_sample_count",
    )
    list_filter = (GeocodedFilter, "state", "geocode_source")
    search_fields = ("truckstop_name", "city", "address", "opis_truckstop_id", "rack_id")
    ordering = ("state", "city", "truckstop_name")
    list_per_page = 50
    readonly_fields = ("created_at", "updated_at", "geocoded_at")
    fieldsets = (
        ("Identity", {"fields": ("opis_truckstop_id", "rack_id", "truckstop_name")}),
        (
            "Location",
            {
                "fields": (
                    "address",
                    "city",
                    "state",
                    "latitude",
                    "longitude",
                    "geocode_source",
                    "geocoded_at",
                )
            },
        ),
        ("Pricing", {"fields": ("retail_price", "price_sample_count")}),
        ("Audit", {"fields": ("created_at", "updated_at")}),
    )
