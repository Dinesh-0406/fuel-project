"""Import pipeline: header validation, deduplication and data hygiene."""

from __future__ import annotations

import csv
from decimal import Decimal
from io import StringIO

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from routes.models import FuelStation

pytestmark = pytest.mark.django_db

HEADERS = [
    "OPIS Truckstop ID",
    "Truckstop Name",
    "Address",
    "City",
    "State",
    "Rack ID",
    "Retail Price",
]


def write_csv(tmp_path, rows, headers=None):
    path = tmp_path / "prices.csv"
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(headers if headers is not None else HEADERS)
        writer.writerows(rows)
    return path


def run_import(path, **kwargs):
    out = StringIO()
    call_command("import_fuel_prices", str(path), stdout=out, stderr=StringIO(), **kwargs)
    return out.getvalue()


def test_imports_a_clean_file(tmp_path):
    path = write_csv(
        tmp_path,
        [
            ["7", "WOODSHED", "I-44, EXIT 283", "Big Cabin", "OK", "307", "3.00733333"],
            ["9", "KWIK TRIP", "I-94, EXIT 143", "Tomah", "WI", "420", "3.28733333"],
        ],
    )
    run_import(path)

    assert FuelStation.objects.count() == 2
    station = FuelStation.objects.get(opis_truckstop_id="7")
    assert station.truckstop_name == "WOODSHED"
    assert station.state == "OK"
    assert station.retail_price == Decimal("3.007333")
    assert station.latitude is None  # enrichment is a separate step


def test_missing_headers_are_rejected(tmp_path):
    path = write_csv(tmp_path, [["7", "X"]], headers=["OPIS Truckstop ID", "Truckstop Name"])
    with pytest.raises(CommandError, match="missing required column"):
        run_import(path)


def test_missing_file_is_rejected(tmp_path):
    with pytest.raises(CommandError, match="CSV not found"):
        run_import(tmp_path / "nope.csv")


def test_duplicate_ids_collapse_and_prices_are_averaged(tmp_path):
    """Repeated rows for one station are repeated price observations."""
    path = write_csv(
        tmp_path,
        [
            ["105", "TA SAGINAW", "I-75, EXIT 149", "Saginaw", "MI", "260", "3.00"],
            ["105", "TA SAGINAW", "I-75, EXIT 149", "Saginaw", "MI", "260", "3.20"],
            ["105", "TA SAGINAW", "I-75, EXIT 149", "Saginaw", "MI", "260", "3.40"],
        ],
    )
    run_import(path)

    station = FuelStation.objects.get(opis_truckstop_id="105")
    assert FuelStation.objects.count() == 1
    assert station.retail_price == Decimal("3.200000")
    assert station.price_sample_count == 3


def test_the_most_descriptive_name_survives_deduplication(tmp_path):
    path = write_csv(
        tmp_path,
        [
            ["20", "PILOT #1243", "I-8, EXIT 119", "Gila Bend", "AZ", "930", "3.899"],
            ["20", "PILOT TRAVEL CENTER #1243", "I-8, EXIT 119", "Gila Bend", "AZ", "930", "3.899"],
        ],
    )
    run_import(path)
    assert FuelStation.objects.get(opis_truckstop_id="20").truckstop_name == (
        "PILOT TRAVEL CENTER #1243"
    )


def test_canadian_rows_are_skipped_by_default(tmp_path):
    path = write_csv(
        tmp_path,
        [
            ["1", "US STOP", "I-90", "Buffalo", "NY", "100", "3.50"],
            ["2", "CA STOP", "HWY 401", "Toronto", "ON", "101", "3.50"],
            ["3", "CA STOP 2", "HWY 1", "Calgary", "AB", "102", "3.50"],
        ],
    )
    output = run_import(path)

    assert FuelStation.objects.count() == 1
    assert FuelStation.objects.get().state == "NY"
    assert "skipped: outside the USA" in output


def test_canadian_rows_can_be_opted_in(tmp_path):
    path = write_csv(
        tmp_path,
        [
            ["1", "US STOP", "I-90", "Buffalo", "NY", "100", "3.50"],
            ["2", "CA STOP", "HWY 401", "Toronto", "ON", "101", "3.50"],
        ],
    )
    run_import(path, include_non_us=True)
    assert FuelStation.objects.count() == 2


@pytest.mark.parametrize("bad_price", ["", "N/A", "abc", "-1.00", "0.10", "999.00"])
def test_unusable_prices_are_skipped_not_defaulted(tmp_path, bad_price):
    path = write_csv(
        tmp_path,
        [
            ["1", "GOOD", "I-90", "Buffalo", "NY", "100", "3.50"],
            ["2", "BAD", "I-90", "Albany", "NY", "101", bad_price],
        ],
    )
    run_import(path)

    assert FuelStation.objects.count() == 1
    assert FuelStation.objects.get().opis_truckstop_id == "1"


def test_currency_formatting_is_parsed(tmp_path):
    path = write_csv(tmp_path, [["1", "GOOD", "I-90", "Buffalo", "NY", "100", "$3,.50"]])
    run_import(path)
    assert FuelStation.objects.get().retail_price == Decimal("3.500000")


def test_rows_missing_city_or_state_are_skipped(tmp_path):
    path = write_csv(
        tmp_path,
        [
            ["1", "GOOD", "I-90", "Buffalo", "NY", "100", "3.50"],
            ["2", "NO CITY", "I-90", "", "NY", "101", "3.50"],
            ["3", "NO STATE", "I-90", "Albany", "", "102", "3.50"],
            ["4", "BAD STATE", "I-90", "Albany", "XYZ", "103", "3.50"],
            ["", "NO ID", "I-90", "Albany", "NY", "104", "3.50"],
        ],
    )
    run_import(path)
    assert list(FuelStation.objects.values_list("opis_truckstop_id", flat=True)) == ["1"]


def test_import_is_idempotent(tmp_path):
    path = write_csv(tmp_path, [["1", "GOOD", "I-90", "Buffalo", "NY", "100", "3.50"]])
    run_import(path)
    run_import(path)

    assert FuelStation.objects.count() == 1
    assert FuelStation.objects.get().retail_price == Decimal("3.500000")


def test_reimport_updates_prices_but_preserves_coordinates(tmp_path):
    path = write_csv(tmp_path, [["1", "GOOD", "I-90", "Buffalo", "NY", "100", "3.50"]])
    run_import(path)

    station = FuelStation.objects.get()
    station.latitude, station.longitude = 42.88, -78.87
    station.geocode_source = "census_place"
    station.save()

    updated = write_csv(tmp_path, [["1", "GOOD", "I-90", "Buffalo", "NY", "100", "4.25"]])
    run_import(updated)

    station.refresh_from_db()
    assert station.retail_price == Decimal("4.250000")
    assert (station.latitude, station.longitude) == (42.88, -78.87)


def test_dry_run_writes_nothing(tmp_path):
    path = write_csv(tmp_path, [["1", "GOOD", "I-90", "Buffalo", "NY", "100", "3.50"]])
    output = run_import(path, dry_run=True)
    assert FuelStation.objects.count() == 0
    assert "Dry run" in output


def test_state_is_normalised_to_uppercase(tmp_path):
    path = write_csv(tmp_path, [["1", "GOOD", "I-90", "Buffalo", "ny", "100", "3.50"]])
    run_import(path)
    assert FuelStation.objects.get().state == "NY"
