"""Import the OPIS fuel-price CSV into the local station table.

Data-quality handling (see README "Dataset findings" for the numbers):

* ``OPIS Truckstop ID`` is the natural key. The source file repeats it -- 8,151
  rows collapse to 6,738 stations -- but address, city and state are always
  identical for a given ID, so collapsing is safe.
* Repeated rows for one ID are repeated PRICE OBSERVATIONS. They are averaged,
  and the sample count is retained so the aggregation stays visible.
* Where the trading name differs between rows for one ID ("PILOT #1243" vs
  "PILOT TRAVEL CENTER #1243") the most descriptive (longest) name is kept.
* Canadian rows (ON, AB, BC, ...) are skipped by default: the assignment is
  restricted to routes inside the USA.

The command is idempotent -- re-running it converges on the same table.
"""

from __future__ import annotations

import csv
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from routes.models import FuelStation

EXPECTED_HEADERS = [
    "OPIS Truckstop ID",
    "Truckstop Name",
    "Address",
    "City",
    "State",
    "Rack ID",
    "Retail Price",
]

CANADIAN_PROVINCES = frozenset(
    {"AB", "BC", "MB", "NB", "NL", "NS", "NT", "NU", "ON", "PE", "QC", "SK", "YT"}
)

# Anything outside this band is a data error rather than a real fuel price.
MIN_PLAUSIBLE_PRICE = Decimal("0.50")
MAX_PLAUSIBLE_PRICE = Decimal("25.00")

PRICE_PRECISION = Decimal("0.000001")


@dataclass
class _Aggregate:
    opis_truckstop_id: str
    truckstop_name: str
    address: str
    city: str
    state: str
    rack_id: str
    prices: list[Decimal] = field(default_factory=list)

    @property
    def average_price(self) -> Decimal:
        total = sum(self.prices, Decimal("0"))
        return (total / Decimal(len(self.prices))).quantize(PRICE_PRECISION)


class Command(BaseCommand):
    help = "Import the OPIS fuel-price CSV into the FuelStation table."

    def add_arguments(self, parser):
        parser.add_argument("csv_path", type=str, help="Path to the fuel-price CSV.")
        parser.add_argument(
            "--include-non-us",
            action="store_true",
            help="Also import Canadian rows (skipped by default).",
        )
        parser.add_argument(
            "--batch-size", type=int, default=1000, help="Bulk operation batch size."
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Parse and report without writing to the database.",
        )

    def handle(self, *args, **options):
        path = Path(options["csv_path"]).expanduser()
        if not path.is_file():
            raise CommandError(f"CSV not found: {path}")

        aggregates, stats = self._parse(path, include_non_us=options["include_non_us"])

        self.stdout.write("")
        self.stdout.write(self.style.MIGRATE_HEADING("Parse summary"))
        for label, value in stats.items():
            self.stdout.write(f"  {label:<34} {value}")
        self.stdout.write(f"  {'unique stations':<34} {len(aggregates)}")

        if options["dry_run"]:
            self.stdout.write(self.style.WARNING("\nDry run - nothing written."))
            return

        created, updated = self._write(aggregates, options["batch_size"])

        self.stdout.write("")
        self.stdout.write(self.style.MIGRATE_HEADING("Database"))
        self.stdout.write(f"  {'created':<34} {created}")
        self.stdout.write(f"  {'updated':<34} {updated}")
        self.stdout.write(f"  {'total in table':<34} {FuelStation.objects.count()}")
        self.stdout.write(
            self.style.SUCCESS("\nImport complete. Next: python manage.py enrich_fuel_stations")
        )

    # -- parsing ------------------------------------------------------------

    def _parse(
        self, path: Path, *, include_non_us: bool
    ) -> tuple[dict[str, _Aggregate], dict[str, int]]:
        stats: dict[str, int] = defaultdict(int)
        aggregates: dict[str, _Aggregate] = {}

        with path.open(newline="", encoding="utf-8-sig") as handle:
            reader = csv.DictReader(handle)
            headers = [h.strip() for h in (reader.fieldnames or [])]
            missing = [h for h in EXPECTED_HEADERS if h not in headers]
            if missing:
                raise CommandError("CSV is missing required column(s): " + ", ".join(missing))

            for row_number, row in enumerate(reader, start=2):
                stats["rows read"] += 1
                opis_id = (row.get("OPIS Truckstop ID") or "").strip()
                state = (row.get("State") or "").strip().upper()
                city = (row.get("City") or "").strip()
                name = (row.get("Truckstop Name") or "").strip()

                if not opis_id:
                    stats["skipped: missing OPIS ID"] += 1
                    continue
                if not state or len(state) != 2:
                    stats["skipped: invalid state"] += 1
                    continue
                if not city:
                    stats["skipped: missing city"] += 1
                    continue
                if state in CANADIAN_PROVINCES and not include_non_us:
                    stats["skipped: outside the USA"] += 1
                    continue

                price = self._parse_price(row.get("Retail Price"))
                if price is None:
                    stats["skipped: invalid price"] += 1
                    self.stderr.write(
                        self.style.WARNING(
                            f"  row {row_number}: unusable price "
                            f"{row.get('Retail Price')!r} for station {opis_id}"
                        )
                    )
                    continue

                existing = aggregates.get(opis_id)
                if existing is None:
                    aggregates[opis_id] = _Aggregate(
                        opis_truckstop_id=opis_id,
                        truckstop_name=name,
                        address=(row.get("Address") or "").strip(),
                        city=city,
                        state=state,
                        rack_id=(row.get("Rack ID") or "").strip(),
                        prices=[price],
                    )
                else:
                    stats["duplicate rows merged"] += 1
                    existing.prices.append(price)
                    # Prefer the most descriptive trading name.
                    if len(name) > len(existing.truckstop_name):
                        existing.truckstop_name = name

        stats["rows accepted"] = sum(len(a.prices) for a in aggregates.values())
        return aggregates, dict(stats)

    @staticmethod
    def _parse_price(raw: str | None) -> Decimal | None:
        if raw is None:
            return None
        text = str(raw).strip().replace("$", "").replace(",", "")
        if not text:
            return None
        try:
            price = Decimal(text)
        except (InvalidOperation, ValueError):
            return None
        if not price.is_finite() or not (MIN_PLAUSIBLE_PRICE <= price <= MAX_PLAUSIBLE_PRICE):
            return None
        return price.quantize(PRICE_PRECISION)

    # -- persistence --------------------------------------------------------

    @transaction.atomic
    def _write(self, aggregates: dict[str, _Aggregate], batch_size: int) -> tuple[int, int]:
        existing = {
            station.opis_truckstop_id: station
            for station in FuelStation.objects.filter(opis_truckstop_id__in=list(aggregates))
        }

        to_create: list[FuelStation] = []
        to_update: list[FuelStation] = []

        for opis_id, aggregate in aggregates.items():
            price = aggregate.average_price
            station = existing.get(opis_id)
            if station is None:
                to_create.append(
                    FuelStation(
                        opis_truckstop_id=opis_id,
                        truckstop_name=aggregate.truckstop_name,
                        address=aggregate.address,
                        city=aggregate.city,
                        state=aggregate.state,
                        rack_id=aggregate.rack_id,
                        retail_price=price,
                        price_sample_count=len(aggregate.prices),
                    )
                )
                continue

            # Re-import updates prices and metadata but preserves coordinates
            # already produced by enrich_fuel_stations.
            station.truckstop_name = aggregate.truckstop_name
            station.address = aggregate.address
            station.city = aggregate.city
            station.state = aggregate.state
            station.rack_id = aggregate.rack_id
            station.retail_price = price
            station.price_sample_count = len(aggregate.prices)
            # auto_now does not fire for bulk_update, so set it explicitly.
            station.updated_at = timezone.now()
            to_update.append(station)

        FuelStation.objects.bulk_create(to_create, batch_size=batch_size)
        if to_update:
            FuelStation.objects.bulk_update(
                to_update,
                [
                    "truckstop_name",
                    "address",
                    "city",
                    "state",
                    "rack_id",
                    "retail_price",
                    "price_sample_count",
                    "updated_at",
                ],
                batch_size=batch_size,
            )
        return len(to_create), len(to_update)
