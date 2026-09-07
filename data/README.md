# Data directory

## `fuel-prices-for-be-assessment.csv`

The dataset supplied with the assignment. **8,151 rows**, columns:

```
OPIS Truckstop ID, Truckstop Name, Address, City, State, Rack ID, Retail Price
```

`Retail Price` is US dollars per gallon. There is **no latitude or longitude** — see the
README section "Why station coordinates are preprocessed" for what follows from that.

Load it with:

```bash
python manage.py import_fuel_prices data/fuel-prices-for-be-assessment.csv
```

The path is a command argument; the file is never referenced from Python source.

### Shape of the data

| Property | Value |
|---|---|
| Rows | 8,151 |
| Unique `OPIS Truckstop ID` | 6,738 |
| Canadian rows (excluded by default) | 620 |
| **US stations after deduplication** | **6,626** |
| Addresses that are highway-exit descriptors | 7,877 (96.6%) |
| Addresses starting with a street number | 8 (0.1%) |
| IDs with more than one price observation | 597 |
| IDs with more than one trading name | 226 |
| Missing values | 0 |
| Invalid prices | 0 |
| Price range | $2.687 – $6.399 (median $3.432) |

`OPIS Truckstop ID` is a safe natural key: address, city and state are always identical
for a given ID. Only the trading name and the price vary between duplicate rows, so the
import collapses on the ID, keeps the most descriptive name and averages the prices.

## `geocache/` (generated, gitignored)

Created by `python manage.py enrich_fuel_stations`:

| File | Purpose |
|---|---|
| `2024_Gaz_place_national.txt` | US Census Gazetteer places — downloaded once. |
| `2024_Gaz_cousubs_national.txt` | US Census Gazetteer county subdivisions — downloaded once. |
| `nominatim_cache.json` | Every Nominatim answer, including misses. Makes reruns free and the command resumable. |
| `geocode_failures.csv` | Stations that could not be resolved (6 of 6,626). |

Safe to delete — everything is regenerated on the next run, and stations already carrying
coordinates are skipped.
