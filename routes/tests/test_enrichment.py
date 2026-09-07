"""Offline coordinate enrichment.

The Census downloads are stubbed by pre-seeding the on-disk cache, so these
tests exercise the real matching logic without touching the network.
"""

from __future__ import annotations

from io import StringIO

import pytest
from django.core.management import call_command

from routes.management.commands.enrich_fuel_stations import (
    normalize_place,
    strip_legal_suffix,
)
from routes.models import FuelStation, GeocodeSource

pytestmark = pytest.mark.django_db

PLACE_ROWS = [
    ("AL", "Abbeville city", "31.564706", "-85.259121"),
    ("MI", "Bay City city", "43.594623", "-83.900161"),
    ("NV", "Carson City", "39.152850", "-119.746696"),
    ("MO", "Saint Louis city", "38.635699", "-90.244582"),
    ("WI", "DeForest village", "43.230291", "-89.343156"),
    ("TX", "Dallas city", "32.793939", "-96.766470"),
]

COUSUB_ROWS = [
    ("NH", "Bow town", "43.128383", "-71.552483"),
    ("MI", "Canton charter township", "42.306797", "-83.482021"),
    ("GA", "Athens-Clarke County unified government", "33.949614", "-83.376395"),
]


def seed_gazetteer(cache_dir, year="2024"):
    """Write the two Gazetteer extracts the command expects to find cached."""
    for stem, rows in (
        (f"{year}_Gaz_place_national", PLACE_ROWS),
        (f"{year}_Gaz_cousubs_national", COUSUB_ROWS),
    ):
        lines = ["USPS\tGEOID\tNAME\tINTPTLAT\tINTPTLONG"]
        lines += [f"{s}\t000\t{n}\t{lat}\t{lon}" for s, n, lat, lon in rows]
        (cache_dir / f"{stem}.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")


def run_enrich(cache_dir, **kwargs):
    out, err = StringIO(), StringIO()
    call_command("enrich_fuel_stations", cache_dir=str(cache_dir), stdout=out, stderr=err, **kwargs)
    return out.getvalue()


# ---------------------------------------------------------------------------
# Name normalisation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Abbeville city", "Abbeville"),
        ("Bay City city", "Bay City"),  # only the final suffix is stripped
        ("Carson City", "Carson City"),  # no lowercase suffix to strip
        ("Autaugaville CCD", "Autaugaville"),
        ("Abanda CDP", "Abanda"),
        ("Canton charter township", "Canton"),
        ("Athens-Clarke County unified government", "Athens-Clarke County"),
        ("Bow town", "Bow"),
    ],
)
def test_legal_suffixes_are_stripped_conservatively(raw, expected):
    assert strip_legal_suffix(raw) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Saint Louis", "ST LOUIS"),
        ("St. Louis", "ST LOUIS"),
        ("Mount Vernon", "MT VERNON"),
        ("Fort Worth", "FT WORTH"),
        ("Cañon City", "CANON CITY"),
        ("  Big   Cabin ", "BIG CABIN"),
    ],
)
def test_place_names_normalise_to_a_common_form(raw, expected):
    assert normalize_place(raw) == expected


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------


def test_stations_are_matched_against_census_places(make_station, tmp_path):
    seed_gazetteer(tmp_path)
    station = make_station(city="Dallas", state="TX", latitude=None, longitude=None)

    run_enrich(tmp_path)

    station.refresh_from_db()
    assert station.latitude == pytest.approx(32.793939)
    assert station.longitude == pytest.approx(-96.766470)
    assert station.geocode_source == GeocodeSource.CENSUS_PLACE.value
    assert station.geocoded_at is not None


def test_city_suffix_names_match_correctly(make_station, tmp_path):
    """ "Bay City" must match "Bay City city", not a place called "Bay"."""
    seed_gazetteer(tmp_path)
    station = make_station(city="Bay City", state="MI", latitude=None, longitude=None)

    run_enrich(tmp_path)

    station.refresh_from_db()
    assert station.latitude == pytest.approx(43.594623)


def test_township_names_fall_back_to_county_subdivisions(make_station, tmp_path):
    seed_gazetteer(tmp_path)
    station = make_station(city="Canton", state="MI", latitude=None, longitude=None)

    run_enrich(tmp_path)

    station.refresh_from_db()
    assert station.geocode_source == GeocodeSource.CENSUS_COUSUB.value
    assert station.latitude == pytest.approx(42.306797)


def test_abbreviation_differences_still_match(make_station, tmp_path):
    seed_gazetteer(tmp_path)
    station = make_station(city="St. Louis", state="MO", latitude=None, longitude=None)

    run_enrich(tmp_path)

    station.refresh_from_db()
    assert station.latitude == pytest.approx(38.635699)


def test_spacing_variants_match_via_the_squashed_index(make_station, tmp_path):
    """The CSV says "De Forest"; the Gazetteer says "DeForest village"."""
    seed_gazetteer(tmp_path)
    station = make_station(city="De Forest", state="WI", latitude=None, longitude=None)

    run_enrich(tmp_path)

    station.refresh_from_db()
    assert station.latitude == pytest.approx(43.230291)


def test_state_scoping_prevents_cross_state_matches(make_station, tmp_path):
    """ "Dallas" exists in the fixture only for TX, so a Dallas, OR must not match."""
    seed_gazetteer(tmp_path)
    station = make_station(city="Dallas", state="OR", latitude=None, longitude=None)

    run_enrich(tmp_path)

    station.refresh_from_db()
    assert station.latitude is None


# ---------------------------------------------------------------------------
# Resumability and reporting
# ---------------------------------------------------------------------------


def test_already_geocoded_stations_are_skipped(make_station, tmp_path):
    seed_gazetteer(tmp_path)
    station = make_station(city="Dallas", state="TX", latitude=1.0, longitude=2.0)

    output = run_enrich(tmp_path)

    station.refresh_from_db()
    assert (station.latitude, station.longitude) == (1.0, 2.0)
    assert "Nothing to do" in output


def test_force_regeocodes_existing_coordinates(make_station, tmp_path):
    seed_gazetteer(tmp_path)
    station = make_station(city="Dallas", state="TX", latitude=1.0, longitude=2.0)

    run_enrich(tmp_path, force=True)

    station.refresh_from_db()
    assert station.latitude == pytest.approx(32.793939)


def test_unresolved_stations_are_kept_and_logged(make_station, tmp_path):
    seed_gazetteer(tmp_path)
    make_station(city="Nowheresville", state="ZZ", latitude=None, longitude=None, opis_id="lost")

    run_enrich(tmp_path)

    station = FuelStation.objects.get(opis_truckstop_id="lost")
    assert station.latitude is None  # kept, but not usable for routing
    failures = (tmp_path / "geocode_failures.csv").read_text()
    assert "Nowheresville" in failures


def test_summary_reports_coverage(make_station, tmp_path):
    seed_gazetteer(tmp_path)
    make_station(city="Dallas", state="TX", latitude=None, longitude=None, opis_id="a")
    make_station(city="Nowheresville", state="ZZ", latitude=None, longitude=None, opis_id="b")

    output = run_enrich(tmp_path)

    assert "processed this run" in output
    assert "50.0%" in output  # one of two resolved


def test_ungeocoded_stations_are_excluded_from_routing_queries(make_station, tmp_path):
    make_station(city="A", state="TX", latitude=None, longitude=None, opis_id="none")
    make_station(city="B", state="TX", latitude=32.0, longitude=-96.0, opis_id="has")

    ids = list(FuelStation.objects.geocoded().values_list("opis_truckstop_id", flat=True))
    assert ids == ["has"]
