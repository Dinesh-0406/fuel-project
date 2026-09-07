"""Resolve fuel-station coordinates once, offline.

Why this command exists
-----------------------
The supplied CSV has no latitude/longitude, and 96.6% of its ``Address`` values
are highway-exit descriptors ("I-44, EXIT 283 & US-69") rather than street
addresses. No address geocoder can resolve those, and geocoding 6,700 stations
at request time would be both unusably slow and an abuse of free services.

So coordinates are resolved ONCE, here, and stored. The API then does pure local
geometry. The strategy is tiered, cheapest and most reliable first:

  Tier 1  US Census Gazetteer "places"            1 bulk download
  Tier 2  US Census Gazetteer "county subdivisions"  1 bulk download
          (covers New England towns and Midwest townships that are not places)
  Tier 3  Nominatim, city+state only, rate-limited, opt-in via --use-nominatim
          (only for the ~140 unincorporated communities the Gazetteer misses)

Tiers 1 and 2 resolve ~96% of stations from TWO HTTP requests total. Tier 3 is
opt-in and touches only the remainder, one request per second per the Nominatim
usage policy.

Accuracy: coordinates are municipality centroids, not building rooftops. That is
the correct precision for this dataset -- the source gives no rooftop-resolvable
address -- and it is why the route corridor is a configurable width rather than
a hairline. This is documented in the README under "Known limitations".

The command is resumable and idempotent: stations that already have coordinates
are skipped, and every Nominatim answer (including failures) is cached on disk.
"""

from __future__ import annotations

import csv
import io
import json
import re
import time
import unicodedata
import zipfile
from dataclasses import dataclass
from pathlib import Path

import requests
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from routes.models import FuelStation, GeocodeSource

# Suffixes the Gazetteer appends to place names ("Abbeville city", "Autauga CCD").
LEGAL_SUFFIXES = [
    "consolidated government",
    "metropolitan government",
    "urban county government",
    "unified government",
    "metro government",
    "city and borough",
    "charter township",
    "municipality",
    "reservation",
    "comunidad",
    "zona urbana",
    "plantation",
    "township",
    "borough",
    "village",
    "precinct",
    "district",
    "division",
    "purchase",
    "location",
    "grant",
    "town",
    "city",
    "CDP",
    "CCD",
    "UT",
]

_NON_ALNUM = re.compile(r"[^A-Z0-9 ]")
_SPACES = re.compile(r"\s+")

# Common abbreviation differences between the CSV and the Gazetteer.
_ABBREVIATIONS = ((r"\bSAINT\b", "ST"), (r"\bMOUNT\b", "MT"), (r"\bFORT\b", "FT"))


def normalize_place(name: str) -> str:
    text = unicodedata.normalize("NFKD", name or "")
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = _NON_ALNUM.sub(" ", text.upper())
    for pattern, replacement in _ABBREVIATIONS:
        text = re.sub(pattern, replacement, text)
    return _SPACES.sub(" ", text).strip()


def strip_legal_suffix(name: str) -> str:
    """Remove at most two trailing legal suffixes.

    Two, because names like "Athens-Clarke County unified government" carry a
    compound suffix. Matching is case-sensitive and anchored to whole words so
    "Bay City city" loses only the final "city".
    """
    result = name.strip()
    for _ in range(2):
        for suffix in LEGAL_SUFFIXES:
            if result.endswith(" " + suffix):
                result = result[: -(len(suffix) + 1)].strip()
                break
        else:
            break
    return result


@dataclass
class _Gazetteer:
    """(state, normalised name) -> coordinates, with a spaceless fallback index."""

    exact: dict[tuple[str, str], tuple[float, float, str]]
    squashed: dict[tuple[str, str], tuple[float, float, str]]

    def lookup(self, city: str, state: str) -> tuple[float, float, str] | None:
        key = normalize_place(city)
        hit = self.exact.get((state, key))
        if hit is not None:
            return hit
        return self.squashed.get((state, key.replace(" ", "")))


class Command(BaseCommand):
    help = "Resolve fuel-station coordinates offline using US Census Gazetteer data."

    def add_arguments(self, parser):
        parser.add_argument(
            "--use-nominatim",
            action="store_true",
            help="Resolve Gazetteer misses via Nominatim (rate-limited, opt-in).",
        )
        parser.add_argument(
            "--force",
            action="store_true",
            help="Re-geocode stations that already have coordinates.",
        )
        parser.add_argument(
            "--limit", type=int, default=0, help="Only process N stations (for testing)."
        )
        parser.add_argument(
            "--cache-dir",
            type=str,
            default="",
            help="Where to keep downloaded reference data (default: <BASE_DIR>/data/geocache).",
        )
        parser.add_argument(
            "--nominatim-delay",
            type=float,
            default=1.1,
            help="Seconds between Nominatim requests (usage policy: >= 1).",
        )

    def handle(self, *args, **options):
        cache_dir = Path(options["cache_dir"] or (settings.BASE_DIR / "data" / "geocache"))
        cache_dir.mkdir(parents=True, exist_ok=True)

        queryset = FuelStation.objects.all()
        if not options["force"]:
            queryset = queryset.filter(latitude__isnull=True)
        queryset = queryset.order_by("state", "city", "opis_truckstop_id")
        if options["limit"]:
            queryset = queryset[: options["limit"]]

        stations = list(queryset)
        total_in_table = FuelStation.objects.count()
        if not stations:
            self.stdout.write(
                self.style.SUCCESS(
                    f"Nothing to do - all {total_in_table} station(s) already have "
                    "coordinates. Use --force to re-geocode."
                )
            )
            return

        self.stdout.write(f"Stations needing coordinates: {len(stations)}")
        gazetteer = self._load_gazetteer(cache_dir)

        resolved: list[FuelStation] = []
        unresolved: list[FuelStation] = []
        counts = {"census_place": 0, "census_cousub": 0, "nominatim": 0}
        now = timezone.now()

        for station in stations:
            hit = gazetteer.lookup(station.city, station.state)
            if hit is None:
                unresolved.append(station)
                continue
            latitude, longitude, source = hit
            station.latitude = latitude
            station.longitude = longitude
            station.geocode_source = source
            station.geocoded_at = now
            station.updated_at = now
            counts[source] += 1
            resolved.append(station)

        self.stdout.write(
            f"Gazetteer resolved {len(resolved)}/{len(stations)} "
            f"({len(resolved) / len(stations) * 100:.1f}%)"
        )

        if unresolved and options["use_nominatim"]:
            still_missing = self._resolve_with_nominatim(
                unresolved, cache_dir, options["nominatim_delay"], counts, now
            )
            resolved.extend(s for s in unresolved if s.has_coordinates)
            unresolved = still_missing

        if resolved:
            with transaction.atomic():
                FuelStation.objects.bulk_update(
                    resolved,
                    ["latitude", "longitude", "geocode_source", "geocoded_at", "updated_at"],
                    batch_size=1000,
                )

        self._write_failure_log(cache_dir, unresolved)
        self._report(stations, resolved, unresolved, counts, total_in_table, cache_dir)

    # -- reference data -----------------------------------------------------

    def _load_gazetteer(self, cache_dir: Path) -> _Gazetteer:
        exact: dict[tuple[str, str], tuple[float, float, str]] = {}
        squashed: dict[tuple[str, str], tuple[float, float, str]] = {}
        year = settings.CENSUS_GAZETTEER_YEAR

        # Places take priority over county subdivisions: an incorporated city is
        # a tighter centroid than the township that contains it.
        sources = [
            (f"{year}_Gaz_place_national", GeocodeSource.CENSUS_PLACE.value, 0),
            (f"{year}_Gaz_cousubs_national", GeocodeSource.CENSUS_COUSUB.value, 1),
        ]
        priority: dict[tuple[str, str], int] = {}

        for stem, source, rank in sources:
            rows = self._read_gazetteer_file(cache_dir, stem, year)
            for row in rows:
                state = (row.get("USPS") or "").strip().upper()
                name = (row.get("NAME") or "").strip()
                try:
                    latitude = float(row["INTPTLAT"])
                    longitude = float(row["INTPTLONG"])
                except (KeyError, TypeError, ValueError):
                    continue
                if not state or not name:
                    continue

                for candidate in {normalize_place(name), normalize_place(strip_legal_suffix(name))}:
                    if not candidate:
                        continue
                    key = (state, candidate)
                    if key not in priority or rank < priority[key]:
                        exact[key] = (latitude, longitude, source)
                        priority[key] = rank
                    squashed.setdefault(
                        (state, candidate.replace(" ", "")), (latitude, longitude, source)
                    )

        self.stdout.write(f"Gazetteer index: {len(exact)} place keys")
        return _Gazetteer(exact=exact, squashed=squashed)

    def _read_gazetteer_file(self, cache_dir: Path, stem: str, year: str) -> list[dict]:
        """Fetch (once) and parse one Gazetteer file, caching the extract on disk."""
        local = cache_dir / f"{stem}.txt"
        if not local.exists():
            url = f"{settings.CENSUS_GAZETTEER_BASE_URL}/{year}_Gazetteer/{stem}.zip"
            self.stdout.write(f"Downloading {url}")
            try:
                response = requests.get(
                    url,
                    timeout=180,
                    headers={"User-Agent": settings.GEOCODER_USER_AGENT},
                )
                response.raise_for_status()
            except requests.RequestException as exc:
                raise CommandError(
                    f"Could not download the Census Gazetteer file {stem}: {exc}"
                ) from exc

            try:
                with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
                    name = next(n for n in archive.namelist() if n.endswith(".txt"))
                    local.write_bytes(archive.read(name))
            except (zipfile.BadZipFile, StopIteration) as exc:
                raise CommandError(f"Gazetteer archive {stem} is unreadable: {exc}") from exc
            self.stdout.write(self.style.SUCCESS(f"  cached -> {local}"))
        else:
            self.stdout.write(f"Using cached {local.name}")

        with local.open(encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle, delimiter="\t")
            return [
                {
                    (k.strip() if k else k): (v.strip() if isinstance(v, str) else v)
                    for k, v in row.items()
                }
                for row in reader
            ]

    # -- tier 3 -------------------------------------------------------------

    def _resolve_with_nominatim(
        self,
        stations: list[FuelStation],
        cache_dir: Path,
        delay: float,
        counts: dict[str, int],
        now,
    ) -> list[FuelStation]:
        """Resolve leftovers one city at a time, respecting the usage policy.

        Queries are grouped by (city, state) so several stations in one town cost
        a single request, and every answer -- including a miss -- is persisted so
        an interrupted run resumes without repeating work.
        """
        cache_path = cache_dir / "nominatim_cache.json"
        disk_cache: dict[str, list[float] | None] = {}
        if cache_path.exists():
            try:
                disk_cache = json.loads(cache_path.read_text())
            except json.JSONDecodeError:
                self.stderr.write(self.style.WARNING("Nominatim cache corrupt; ignoring."))

        pending: dict[str, list[FuelStation]] = {}
        for station in stations:
            pending.setdefault(f"{station.city}|{station.state}", []).append(station)

        uncached = [k for k in pending if k not in disk_cache]
        self.stdout.write(
            f"Nominatim: {len(pending)} unique location(s), {len(uncached)} not cached "
            f"(~{len(uncached) * delay / 60:.1f} min)"
        )

        session = requests.Session()
        session.headers.update({"User-Agent": settings.GEOCODER_USER_AGENT})

        for index, key in enumerate(uncached, start=1):
            city, state = key.split("|", 1)
            try:
                response = session.get(
                    f"{settings.NOMINATIM_BASE_URL.rstrip('/')}/search",
                    params={
                        "city": city,
                        "state": state,
                        "country": "USA",
                        "format": "jsonv2",
                        "limit": 1,
                    },
                    timeout=settings.NOMINATIM_TIMEOUT_SECONDS,
                )
                response.raise_for_status()
                payload = response.json()
                disk_cache[key] = (
                    [float(payload[0]["lat"]), float(payload[0]["lon"])] if payload else None
                )
            except (requests.RequestException, ValueError, KeyError, IndexError) as exc:
                self.stderr.write(self.style.WARNING(f"  {key}: {exc}"))
                disk_cache[key] = None

            if index % 25 == 0 or index == len(uncached):
                cache_path.write_text(json.dumps(disk_cache, indent=0))
                self.stdout.write(f"  {index}/{len(uncached)}")
            time.sleep(delay)

        cache_path.write_text(json.dumps(disk_cache, indent=0))

        still_missing: list[FuelStation] = []
        for key, group in pending.items():
            coordinates = disk_cache.get(key)
            if not coordinates:
                still_missing.extend(group)
                continue
            for station in group:
                station.latitude, station.longitude = coordinates[0], coordinates[1]
                station.geocode_source = GeocodeSource.NOMINATIM.value
                station.geocoded_at = now
                station.updated_at = now
                counts["nominatim"] += 1
        return still_missing

    # -- reporting ----------------------------------------------------------

    def _write_failure_log(self, cache_dir: Path, unresolved: list[FuelStation]) -> None:
        path = cache_dir / "geocode_failures.csv"
        if not unresolved:
            path.unlink(missing_ok=True)
            return
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(["opis_truckstop_id", "truckstop_name", "address", "city", "state"])
            for station in unresolved:
                writer.writerow(
                    [
                        station.opis_truckstop_id,
                        station.truckstop_name,
                        station.address,
                        station.city,
                        station.state,
                    ]
                )
        self.stdout.write(self.style.WARNING(f"Unresolved stations logged to {path}"))

    def _report(self, stations, resolved, unresolved, counts, total_in_table, cache_dir):
        geocoded_total = FuelStation.objects.geocoded().count()
        self.stdout.write("")
        self.stdout.write(self.style.MIGRATE_HEADING("Enrichment summary"))
        rows = [
            ("processed this run", len(stations)),
            ("  resolved: Census place", counts["census_place"]),
            ("  resolved: Census county subdivision", counts["census_cousub"]),
            ("  resolved: Nominatim", counts["nominatim"]),
            ("  failed", len(unresolved)),
            ("stations in table", total_in_table),
            ("stations with coordinates", geocoded_total),
        ]
        for label, value in rows:
            self.stdout.write(f"  {label:<38} {value}")
        if total_in_table:
            self.stdout.write(f"  {'coverage':<38} {geocoded_total / total_in_table * 100:.1f}%")
        if unresolved:
            self.stdout.write(
                self.style.WARNING(
                    "\nUnresolved stations are kept in the table but excluded from "
                    "route matching. Re-run with --use-nominatim to resolve more."
                )
            )
        else:
            self.stdout.write(self.style.SUCCESS("\nAll stations have coordinates."))
