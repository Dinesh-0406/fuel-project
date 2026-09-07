"""Local representation of the OPIS fuel-station dataset."""

from __future__ import annotations

from django.db import models


class GeocodeSource(models.TextChoices):
    """How a station's coordinates were obtained during offline enrichment."""

    CENSUS_PLACE = "census_place", "Census Gazetteer (place)"
    CENSUS_COUSUB = "census_cousub", "Census Gazetteer (county subdivision)"
    NOMINATIM = "nominatim", "Nominatim"
    MANUAL = "manual", "Manual override"


class FuelStationQuerySet(models.QuerySet["FuelStation"]):
    def geocoded(self) -> FuelStationQuerySet:
        """Only stations usable for spatial route matching."""
        return self.exclude(latitude__isnull=True).exclude(longitude__isnull=True)

    def in_bounding_box(
        self, min_lat: float, min_lon: float, max_lat: float, max_lon: float
    ) -> FuelStationQuerySet:
        """Index-friendly prefilter used before exact corridor distances."""
        return self.filter(
            latitude__gte=min_lat,
            latitude__lte=max_lat,
            longitude__gte=min_lon,
            longitude__lte=max_lon,
        )


class FuelStation(models.Model):
    """A truck stop with a retail fuel price.

    ``opis_truckstop_id`` is the dataset's natural key: it is stable across the
    duplicate rows in the source CSV (address, city and state never differ for a
    given ID -- only the trading name and repeated price observations do), so the
    import collapses duplicates onto it.

    Coordinates are nullable on purpose. The source CSV has no lat/lon and 96.6%
    of its addresses are highway-exit descriptors ("I-44, EXIT 283 & US-69")
    rather than street addresses, so coordinates are resolved once by the offline
    ``enrich_fuel_stations`` command. Stations that cannot be resolved are kept
    for reporting but excluded from spatial route matching.
    """

    opis_truckstop_id = models.CharField(max_length=32, unique=True, db_index=True)
    truckstop_name = models.CharField(max_length=255)
    address = models.CharField(max_length=255, blank=True)
    city = models.CharField(max_length=128, db_index=True)
    state = models.CharField(max_length=2, db_index=True)
    rack_id = models.CharField(max_length=32, blank=True, db_index=True)

    # Money: Decimal only. Source prices carry up to 8 dp; we retain 6.
    retail_price = models.DecimalField(max_digits=10, decimal_places=6, db_index=True)
    price_sample_count = models.PositiveIntegerField(
        default=1,
        help_text="Number of CSV rows averaged into retail_price for this station.",
    )

    latitude = models.FloatField(null=True, blank=True, db_index=True)
    longitude = models.FloatField(null=True, blank=True, db_index=True)
    geocode_source = models.CharField(max_length=32, choices=GeocodeSource.choices, blank=True)
    geocoded_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = FuelStationQuerySet.as_manager()

    class Meta:
        ordering = ("state", "city", "truckstop_name")
        indexes = [
            # Composite index backing the route bounding-box prefilter.
            models.Index(fields=["latitude", "longitude"], name="fuelstation_lat_lon_idx"),
            models.Index(fields=["state", "city"], name="fuelstation_state_city_idx"),
            models.Index(fields=["retail_price"], name="fuelstation_price_idx"),
        ]
        verbose_name = "fuel station"
        verbose_name_plural = "fuel stations"

    def __str__(self) -> str:
        return f"{self.truckstop_name} ({self.city}, {self.state})"

    @property
    def has_coordinates(self) -> bool:
        return self.latitude is not None and self.longitude is not None
