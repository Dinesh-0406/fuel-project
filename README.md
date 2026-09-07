# Fuel Route Optimizer

A Django REST API that plans a driving route between two US locations and selects the
**cost-optimal** fuel stops along it for a vehicle with a **500-mile range** and
**10 MPG** fuel efficiency.

```
POST /api/v1/routes/   {"start": "New York, NY", "finish": "Los Angeles, CA"}
```

returns the route as map-ready GeoJSON, the chosen fuel stops with the exact number of
gallons to buy at each, and the total fuel spend.

Built with **Django 6.1** and **Django REST Framework**, running on SQLite out of the
box (PostgreSQL via one environment variable).

---

## Table of contents

1. [What it does](#1-what-it-does)
2. [Architecture](#2-architecture)
3. [Why OSRM](#3-why-osrm)
4. [Why station coordinates are preprocessed](#4-why-station-coordinates-are-preprocessed)
5. [Installation](#5-installation)
6. [Environment variables](#6-environment-variables)
7. [Dataset setup](#7-dataset-setup)
8. [Running the server](#8-running-the-server)
9. [The API](#9-the-api)
10. [Fuel optimization explained](#10-fuel-optimization-explained)
11. [What "optimal" means here](#11-what-optimal-means-here)
12. [Performance](#12-performance)
13. [Caching strategy](#13-caching-strategy)
14. [External API assumptions](#14-external-api-assumptions)
15. [Dataset findings](#15-dataset-findings)
16. [Testing](#16-testing)
17. [Security](#17-security)
18. [Known limitations](#18-known-limitations)
19. [Future improvements](#19-future-improvements)
20. [Project structure](#20-project-structure)

---

## 1. What it does

Given two human-readable US locations, the API:

1. geocodes both endpoints (cached aggressively),
2. fetches the driving route from OSRM — **exactly one routing call**,
3. finds every fuel station within a configurable corridor of that route **locally**,
4. computes the cheapest feasible sequence of fuel purchases, and
5. returns the route as a GeoJSON `LineString` plus the fuel plan.

There is also a small Leaflet page at `/` that renders the result on a map.

**The central design constraint:** the routing service is called once per journey and
never per station. Everything about which stations are on the way, how far along the
route each one sits, and which to stop at is local computation over the route polyline.

---

## 2. Architecture

```
                    HTTP
                      │
              ┌───────▼────────┐
              │  RoutePlanView │   thin: validate, cache-check, serialise
              └───────┬────────┘
                      │
              ┌───────▼────────┐
              │  RoutePlanner  │   orchestration + call budget
              └───────┬────────┘
        ┌─────────────┼──────────────┬──────────────────┐
        │             │              │                  │
 ┌──────▼─────┐ ┌─────▼──────┐ ┌─────▼───────┐ ┌────────▼────────┐
 │ Geocoding  │ │  Routing   │ │StationFinder│ │ FuelPlan        │
 │ Provider   │ │  Provider  │ │             │ │ Optimizer       │
 │ (Nominatim)│ │  (OSRM)    │ │  local      │ │  local          │
 └──────┬─────┘ └─────┬──────┘ └─────┬───────┘ └─────────────────┘
        │             │              │
    cached        cached      ┌──────▼───────┐
                              │ RouteGeometry│  resample + grid index
                              └──────┬───────┘
                                     │
                              ┌──────▼───────┐
                              │ FuelStation  │  bbox query on indexed lat/lon
                              │   (ORM)      │
                              └──────────────┘
```

Design rules the code follows:

* **Views are thin.** `RoutePlanView` validates, checks the plan cache and serialises.
  All behaviour lives in `routes/services/`.
* **Serializers validate.** `RouteRequestSerializer` owns request validation.
* **External integrations sit behind interfaces.** `RoutingProvider` and
  `GeocodingProvider` are ABCs; `OSRMProvider` and `NominatimProvider` implement them,
  and `CachedRoutingProvider` / `CachedGeocoder` decorate them. Swapping in Mapbox or
  Valhalla means writing one class.
* **One error envelope.** Every failure passes through `routes/exceptions.py` and comes
  back as `{"error": {"code", "message", "details"}}`. Stack traces never reach a client.

---

## 3. Why OSRM

| | |
|---|---|
| **Free, no API key** | Nothing to provision for a reviewer running this locally. |
| **One call gives everything** | `overview=full&geometries=geojson` returns the full-precision LineString *and* an authoritative driving distance and duration in a single response. |
| **Self-hostable** | The public demo server is rate-limited and unsuitable for production, but the same code points at a private OSRM instance by changing `OSRM_BASE_URL`. No vendor lock-in. |
| **Accurate distances** | Trip distance comes from OSRM's road-network routing, never from straight-line distance. Meters convert to miles with the exact factor 1609.344. |

The provider is used exactly as specified:

```
https://router.project-osrm.org/route/v1/driving/{lon},{lat};{lon},{lat}
    ?overview=full&geometries=geojson&steps=false
```

---

## 4. Why station coordinates are preprocessed

**This is the most important design decision in the project.**

The supplied CSV has **no latitude or longitude**. It has 8,151 rows and an `Address`
column — but inspection shows **96.6% of those addresses are highway-exit descriptors**,
not street addresses:

```
I-44, EXIT 283 & US-69
I-94, EXIT 143 & US-12 & SR-21
I-8, EXIT 119 & SR-85
```

Only **8 rows out of 8,151** begin with a street number. No address geocoder can resolve
the rest, so a naive "geocode the address column" approach fails on essentially the whole
dataset.

Geocoding at request time is off the table regardless: 6,626 lookups per request would
take hours and would abuse a free service. So coordinates are resolved **once, offline**,
by `python manage.py enrich_fuel_stations`, using a tiered strategy:

| Tier | Source | HTTP requests | Resolved |
|---|---|---|---|
| 1 | **US Census Gazetteer — places** (bulk file) | 1 | 6,261 |
| 2 | **US Census Gazetteer — county subdivisions** (bulk file) | 1 | 147 |
| 3 | **Nominatim**, city+state, ≥1 s apart, opt-in `--use-nominatim` | 141 | 212 |
| — | unresolved (kept, flagged, excluded from routing) | — | 6 |

**Total: 143 HTTP requests to coordinate 6,626 stations — and 96.7% of them from just
two bulk file downloads, in about seven seconds.** Final coverage is **99.9%**.

Tier 2 exists because New England towns and Midwest townships (Bow NH, Canton MI) are
county subdivisions rather than incorporated places, so the places file alone misses them.

Matching normalises `Saint`/`St`, `Mount`/`Mt`, `Fort`/`Ft`, strips accents, and strips
Census legal suffixes carefully — only the *final* suffix, so `"Bay City city"` becomes
`"Bay City"` and not `"Bay"`. A spaceless fallback index catches `"De Forest"` vs
`"DeForest"`.

The command is **resumable and idempotent**: already-geocoded stations are skipped, the
downloaded reference files are cached on disk, and every Nominatim answer (including
misses) is persisted to `data/geocache/nominatim_cache.json`, so an interrupted run picks
up where it left off. Failures are written to `data/geocache/geocode_failures.csv`.

**Accuracy trade-off, stated plainly:** these are municipality centroids, not rooftop
coordinates. A station is placed at the centre of its town, typically within a few miles
of its true position. That is the best precision the source data supports, and it is
exactly why the route corridor is a configurable width rather than a hairline. See
[Known limitations](#18-known-limitations).

---

## 5. Installation

Requires Python 3.11+ (developed on 3.13).

```bash
git clone <repository-url>
cd fuel-route-optimizer

python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate

pip install -r requirements.txt        # runtime only
pip install -r requirements-dev.txt    # plus pytest, ruff, black

cp .env.example .env
```

Open `.env` and set `GEOCODER_USER_AGENT` to something that identifies you, including a
real contact address. Nominatim's usage policy requires this and **returns HTTP 403 for
placeholder values** such as `example.com`.

---

## 6. Environment variables

Every setting has a working default — the app runs with no `.env` at all. `.env.example`
documents the full list; the ones that matter:

| Variable | Default | Purpose |
|---|---|---|
| `DJANGO_DEBUG` | `False` | Security settings harden automatically when this is off. |
| `DATABASE_URL` | *(unset)* | Unset → SQLite. Set → PostgreSQL. |
| `REDIS_URL` | *(unset)* | Unset → LocMemCache. Set → Redis. |
| `VEHICLE_MAX_RANGE_MILES` | `500` | Vehicle range. |
| `VEHICLE_MPG` | `10` | Fuel efficiency. Capacity is derived: 500/10 = 50 gal. |
| `FUEL_STATION_CORRIDOR_MILES` | `10` | How far off-route a station may sit. |
| `ROUTE_RESAMPLE_MILES` | `1.0` | Polyline resolution for the corridor search. |
| `OSRM_BASE_URL` | public demo server | Point at your own OSRM in production. |
| `GEOCODER_USER_AGENT` | — | **Must identify you.** Nominatim rejects placeholders. |
| `ROUTE_CACHE_TTL_SECONDS` | `86400` | Route cache lifetime. |
| `API_THROTTLE_ANON` | `60/min` | Per-IP rate limit. |

---

## 7. Dataset setup

Place `fuel-prices-for-be-assessment.csv` in `data/` (a copy is already there), then:

```bash
python manage.py migrate
python manage.py import_fuel_prices data/fuel-prices-for-be-assessment.csv
python manage.py enrich_fuel_stations --use-nominatim
```

The CSV path is an argument — nothing is hard-coded.

**Step 2** parses and normalises the CSV, collapses duplicates and loads 6,626 US
stations. It validates headers, rejects implausible prices, and is idempotent — re-running
it updates prices while **preserving coordinates** already resolved by step 3. Add
`--dry-run` to see the parse report without writing.

**Step 3** resolves coordinates (see [section 4](#4-why-station-coordinates-are-preprocessed)).
Omit `--use-nominatim` to stop after the two bulk downloads at 96.7% coverage in ~7
seconds; include it to reach 99.9% in about three more minutes. Re-running is free — it
skips everything already done.

Expected output:

```
Parse summary
  rows read                          8151
  duplicate rows merged              905
  skipped: outside the USA           620
  unique stations                    6626

Enrichment summary
  resolved: Census place               6261
  resolved: Census county subdivision  147
  resolved: Nominatim                  212
  failed                               6
  coverage                             99.9%
```

Browse the loaded data at `/admin/` (`python manage.py createsuperuser` first) — it has
filters for state, geocoding source and whether coordinates are present.

---

## 8. Running the server

```bash
python manage.py runserver
```

* `http://127.0.0.1:8000/` — Leaflet map demo
* `http://127.0.0.1:8000/api/v1/routes/` — the API
* `http://127.0.0.1:8000/api/docs/` — Swagger UI
* `http://127.0.0.1:8000/admin/` — station admin

---

## 9. The API

### `POST /api/v1/routes/`

```json
{ "start": "New York, NY", "finish": "Los Angeles, CA" }
```

Full state names work too (`"New York, New York"`).

```bash
curl -X POST http://127.0.0.1:8000/api/v1/routes/ \
  -H "Content-Type: application/json" \
  -d '{"start": "New York, NY", "finish": "Los Angeles, CA"}'
```

### Response (abridged — a real one has ~34,500 geometry points)

```json
{
  "start":  { "input": "New York, NY", "latitude": 40.7127281, "longitude": -74.0060152 },
  "finish": { "input": "Los Angeles, CA", "latitude": 34.0536909, "longitude": -118.242766 },
  "route": {
    "distance_miles": 2794.03,
    "duration_minutes": 2987.7,
    "geometry": { "type": "LineString", "coordinates": [[-74.006, 40.712], ...] }
  },
  "vehicle": {
    "max_range_miles": 500.0,
    "fuel_efficiency_mpg": 10.0,
    "tank_capacity_gallons": 50.0,
    "starting_fuel_gallons": 50.0
  },
  "fuel_plan": {
    "stop_count": 15,
    "total_gallons_purchased": 229.403,
    "total_fuel_consumed_gallons": 279.403,
    "fuel_remaining_at_destination_gallons": 0.0,
    "total_cost": "694.02",
    "stops": [
      {
        "sequence": 1,
        "station": {
          "id": 1234, "opis_truckstop_id": "4321",
          "name": "TRAVEL CENTER", "address": "I-80, EXIT 234",
          "city": "Youngstown", "state": "OH",
          "latitude": 41.0997, "longitude": -80.6495
        },
        "price_per_gallon": "3.059",
        "distance_from_start_miles": 390.79,
        "distance_from_previous_stop_miles": 390.79,
        "distance_to_destination_miles": 2403.24,
        "detour_from_route_miles": 3.78,
        "fuel_before_purchase_gallons": 10.921,
        "fuel_purchased_gallons": 5.643,
        "fuel_after_purchase_gallons": 16.564,
        "cost": "17.26"
      }
    ]
  },
  "meta": {
    "routing_provider": "OSRM",
    "geocoding_provider": "Nominatim",
    "route_corridor_miles": 10.0,
    "fuel_station_count_considered": 460,
    "fuel_stations_in_bounding_box": 3406,
    "route_geometry_points": 34513,
    "external_calls": { "geocoding": 2, "routing": 1 },
    "route_cache_hit": false,
    "plan_cache_hit": false,
    "timing_ms": { "total": 1573.1, "routing_provider": 1521.6, "local": 51.5 }
  }
}
```

`meta.external_calls` is deliberately part of the contract: it is how you verify from the
outside that the service is not calling the routing API per station.

### Money and units

* **All monetary values are strings** (`"694.02"`), computed with `Decimal` and rounded
  half-up to 2 dp. No binary float ever touches a price. Per-stop costs sum **exactly** to
  `total_cost`.
* Gallons are rounded to 3 dp, miles to 2 dp.

### Errors

Every error uses one envelope:

```json
{ "error": { "code": "NO_FEASIBLE_FUEL_PLAN", "message": "...", "details": { } } }
```

| Status | Code | When |
|---|---|---|
| 400 | `INVALID_REQUEST` | Empty/missing/overlong field, identical start and finish, malformed JSON. |
| 400 | `LOCATION_OUTSIDE_SERVICE_AREA` | Resolved outside the USA. |
| 404 | `LOCATION_NOT_FOUND` | Location could not be geocoded. |
| 405 | `METHOD_NOT_ALLOWED` | Anything but `POST`. |
| 422 | `NO_ROUTE_FOUND` | OSRM reports no drivable route. |
| 422 | `NO_FEASIBLE_FUEL_PLAN` | No station sequence bridges the trip within 500 miles. |
| 429 | `THROTTLED` | Rate limit exceeded. |
| 502 | `PROVIDER_UNAVAILABLE` | Upstream timeout, connection error or malformed response. |
| 500 | `INTERNAL_ERROR` | Unexpected — logged in full, never exposed. |

---

## 10. Fuel optimization explained

### The problem

The vehicle holds **50 gallons** (500 miles ÷ 10 MPG), starts **full**, and burns
`distance / 10` gallons. Given stations at known distances along the route with known
prices, choose where to stop and **how much to buy at each** so the destination is reached
for the least money.

Note what this is *not*: it is not "pick the cheapest stations". Buying 50 gallons at a
cheap station you reach on fumes may be worse than buying 12 gallons at a dearer one that
lets you reach a much cheaper one later. The purchase *quantity* is as much a decision as
the stop itself.

### The algorithm

This is the classic **gas station problem**, and it has a provably optimal greedy
solution. At each stop, look ahead as far as a full tank can carry you:

* **If a cheaper station is reachable** → buy *just enough* to reach the **first** one.
  There is no reason to buy expensive fuel when cheaper fuel is within range.
* **If nothing cheaper is reachable** → this is the best price for a while, so **fill the
  tank completely** and continue to the **cheapest** station still reachable.
* **If the destination is reachable** and no cheaper station lies before it → buy *exactly*
  enough to arrive, and stop buying.

The start is treated as a stop where the fuel is already owned and no purchase is
possible, so the opening move is simply "drive to the cheapest station within 500 miles".
The destination behaves like a node with free fuel — cheaper than every station — which is
what makes the third rule fall out of the second.

You can watch all three rules in a real NY → LA plan:

```
  7. mi 1334.9 | Waco       NE | $2.799/gal | buy 50.00 gal | tank  0.0 → 50.0   ← cheapest around: fill up
  9. mi 1458.9 | Lexington  NE | $2.979/gal | buy  1.29 gal | tank 48.7 → 50.0   ← top-up to reach cheaper fuel
 15. mi 2516.2 | N Las Vegas NV| $3.282/gal | buy 17.41 gal | tank 10.4 → 27.8   ← only enough to finish
```

### Correctness

Both look-ups are O(1) after O(n log n) preprocessing — a monotonic stack answers "next
cheaper station" and a sparse table answers "cheapest station in a window" — so planning
is O(n log n) with no I/O.

Feasibility is decided *before* the greedy runs, by a forward reachability scan: walk the
stations in order tracking the furthest point reachable if the tank were filled at every
station passed so far. If that never covers the destination, no strategy can succeed and
the API returns `NO_FEASIBLE_FUEL_PLAN`. Because that scan guarantees consecutive stations
are within range, the greedy can never strand itself.

**The optimality claim is tested, not asserted.** `test_greedy_matches_exhaustive_optimum`
compares the greedy against a brute-force dynamic-programming oracle that enumerates
*every* purchase quantity at *every* station and assumes nothing about the shape of the
answer. Instances are built so all quantities are exactly representable (positions in
multiples of 10 miles, 1 gallon = 10 miles), making the comparison exact. Across ~1,000
randomised instances per run — including price ties, exact-500-mile gaps and infeasible
cases — the greedy matches the optimum every time.

---

## 11. What "optimal" means here

**Optimal = minimum total fuel spend**, subject to:

* the vehicle's 500-mile maximum range,
* 10 MPG consumption,
* a full tank at the start,
* only stations within `FUEL_STATION_CORRIDOR_MILES` of the route,
* stations taken in route order,
* the prices in the supplied dataset.

It explicitly does **not** mean the globally cheapest stations in the USA. A station is
only a candidate if the route actually passes near it, so every stop is geographically
sensible. The optimiser also never treats the detour itself as free distance — see
[Known limitations](#18-known-limitations).

---

## 12. Performance

Measured on a 2,794-mile New York → Los Angeles route with 6,620 geocoded stations
(`python manage.py benchmark_routes`):

| | Uncached | Cached |
|---|---|---|
| **Total response** | ~1,200–3,000 ms | **8–80 ms** |
| Waiting on OSRM | ~1,150–2,950 ms | 0 ms |
| **Local computation** | **~30–52 ms** | 0 ms |
| Geocoding calls | 2 | 0 |
| **Routing calls** | **1** | **0** |

Almost all uncached latency is the public OSRM demo server. Local work — parsing 34,513
route points, filtering 6,620 stations to 460 candidates, and optimising — is about
50 ms. A self-hosted OSRM would put uncached responses in the low hundreds of
milliseconds.

Representative live results:

| Route | Distance | Stops | Total | Corridor stations |
|---|---|---|---|---|
| New York, NY → Chicago, IL | 791 mi | 2 | $87.71 | 227 |
| Houston, TX → Chicago, IL | 1,092 mi | 5 | $168.86 | — |
| Los Angeles, CA → Denver, CO | 1,017 mi | 3 | $169.56 | 58 |
| **New York, NY → Los Angeles, CA** | **2,794 mi** | **15** | **$694.02** | **460** |
| Seattle, WA → Miami, FL | 3,302 mi | 18 | $848.32 | 392 |

### How the station search stays fast

Testing 6,620 stations against 34,513 route points is 228 million distance calculations —
far too slow. Three steps avoid it:

1. **Bounding box.** The route's box, expanded by the corridor width, is an indexed SQL
   query on `(latitude, longitude)`. It cuts 6,620 stations to 3,406 in one query.
   (A single database round-trip — asserted by a test.)
2. **Resampling.** The 34,513-point polyline is thinned to ~2,450 points at ~1-mile
   spacing. Each retained point keeps its **exact** cumulative distance, and those
   distances are rescaled so the final value equals OSRM's authoritative route distance.
3. **Grid index.** Retained segments are indexed into 10-mile cells, so each station only
   tests the handful of segments in its own and neighbouring cells — O(1) per station
   rather than O(route points). Segments are recorded in *every* cell their bounding box
   touches, because OSRM emits sparse vertices on long motorway runs and a segment can
   span several cells.

A bounding box alone is not enough: the box around a long diagonal route contains a huge
area the route never passes through. Step 3 is what makes "near the route" mean near the
*route*.

**Geometry assumptions.** Distances use the haversine formula on a spherical Earth. The
point-to-segment projection inside the corridor uses a local equirectangular
approximation, whose error over tens of miles is far below the accuracy of the station
coordinates themselves.

---

## 13. Caching strategy

Three layers, all on Django's cache framework — LocMemCache locally, Redis by setting
`REDIS_URL`.

| Layer | Key | TTL | Effect |
|---|---|---|---|
| **Geocoding** | normalised location string | 30 days | `"New York, NY"`, `" new york , ny "` and `"NEW YORK, NY"` share one entry. Misses are cached for 10 min so a repeated typo can't flood the provider. |
| **Route** | rounded start+finish coordinates + provider | 24 h | A repeat journey never re-calls OSRM. |
| **Plan** | resolved coordinates + corridor + range + MPG + dataset version | 6 h | Serves the finished payload, skipping routing, station search and optimisation entirely. |

Key details:

* **Keys are SHA-256 hashes of normalised values**, never raw user input, so a client
  cannot poison or enumerate the cache with crafted strings.
* **The plan cache is checked after geocoding but before everything else** — geocoding is
  the cheap cached step, and its output is what identifies the journey. This is why a
  cache hit costs 8 ms instead of 50.
* **Coordinates are rounded to 3 dp (~110 m)** before keying, collapsing trivially
  different requests onto one entry.
* **The plan key includes a dataset fingerprint** (station count + latest `updated_at`),
  so re-importing or re-enriching the dataset invalidates every cached plan. The
  fingerprint is itself cached for 5 minutes to keep it off the hot path, so a data reload
  takes effect within 5 minutes rather than instantly — a deliberate trade, since the
  dataset is a static snapshot.
* On a cache hit the response echoes **the caller's own spelling** back, and reports **its
  own timing**, not the timing of the request that populated the cache.

---

## 14. External API assumptions

Providers are assumed to be unreliable, and are wrapped accordingly:

* **Timeouts on every call** — 15 s for OSRM, 10 s for Nominatim, both configurable.
* **Bounded retries on GET only.** GETs are idempotent so a repeat is safe. Retries are
  capped (default 2) with linear backoff — no unbounded loops. Only 429/5xx are retried;
  a 4xx will not improve on a repeat, so it fails immediately.
* **Every failure mode is a domain exception**, not a leaked `requests` error: timeouts,
  connection errors, HTTP errors, malformed JSON, `NoRoute`/`NoSegment`, geometry with
  fewer than two points, zero-length routes and malformed coordinates.
* **No user-controlled URLs.** Provider base URLs come from settings; the request body
  only ever supplies query text. There is no SSRF surface.
* **Latency is logged** for every external call.
* A cached route keeps serving even if the provider subsequently goes down (tested).

### Public-endpoint etiquette

The defaults point at free public infrastructure. Both are fine for a demo and neither is
appropriate for production traffic:

* **OSRM demo server** — rate-limited. Set `OSRM_BASE_URL` to your own instance.
* **Nominatim** — max 1 request/second and a genuine identifying `User-Agent` required.
  The runtime path makes at most 2 calls per uncached request and caches for 30 days; the
  bulk enrichment path sleeps ≥1 s between calls and is opt-in.

---

## 15. Dataset findings

Findings from inspecting the supplied CSV, and how each is handled:

| Finding | Count | Handling |
|---|---|---|
| Total rows | 8,151 | — |
| **Highway-exit "addresses"** rather than street addresses | 7,877 (96.6%) | Drove the whole geocoding design ([section 4](#4-why-station-coordinates-are-preprocessed)). |
| **Canadian rows** (ON, AB, BC, MB, SK, YT, QC, NS, NB) | 620 | Skipped — the assignment is USA-only. `--include-non-us` overrides. |
| **Duplicate `OPIS Truckstop ID`s** | 1,413 rows over 678 IDs | Collapsed. The ID is a safe natural key: address, city and state are *always* identical for a given ID (verified). |
| **Same station, different prices** | 597 IDs | Repeated price observations. Averaged, with `price_sample_count` retained so the aggregation stays visible. |
| **Same station, different trading names** | 226 IDs | e.g. `PILOT #1243` / `PILOT TRAVEL CENTER #1243`. The longest, most descriptive name is kept. |
| Exact duplicate rows | 26 | Absorbed by the same collapse. |
| Missing values in any column | 0 | Validation still rejects blanks defensively. |
| Invalid/implausible prices | 0 | Prices outside $0.50–$25.00 are rejected and logged, never silently defaulted. |
| Unusual state values | 0 malformed | 57 distinct codes; 9 are Canadian provinces (above). 48 US states remain. |
| Price range | $2.687 – $6.399 (median $3.432) | Stored as `Decimal(10,6)`. |

**Net result:** 8,151 rows → **6,626 unique US stations**, 6,620 with coordinates.

No row is silently corrupted: everything skipped is counted and reported, and unusable
prices are written to stderr with their row number.

---

## 16. Testing

```bash
pytest                                    # 160 tests
pytest --cov=routes --cov-report=term-missing
```

**The suite runs fully offline.** OSRM and Nominatim are always mocked; no test depends on
a live service.

| Module | Covers |
|---|---|
| `test_optimizer.py` | Scenarios A–N from the brief, the 500-mile constraint, 10 MPG arithmetic, `Decimal` money, tank capacity, partial starting fuel, **and the exhaustive-DP property test**. |
| `test_geometry.py` | Haversine against known distances, bounding-box expansion, distance rescaling, corridor inclusion/exclusion, resampling trade-offs, sparse-segment grid indexing. |
| `test_station_finder.py` | Corridor width, ordering along the route, ungeocoded exclusion, single-query assertion, 2,000-station performance guard. |
| `test_services.py` | OSRM parsing and parameters, malformed responses, timeouts, retry bounds, no-retry-on-4xx, Nominatim US restriction, all three cache layers. |
| `test_import.py` | Header validation, deduplication, price averaging, bad prices, Canadian skipping, idempotency, coordinate preservation on re-import. |
| `test_enrichment.py` | Suffix stripping (`"Bay City city"` → `"Bay City"`), abbreviation matching, tier fallback, state scoping, resumability, failure logging. |
| `test_api.py` | Request validation, response shape, GeoJSON validity, money serialisation, error codes for every failure mode, **call-budget assertions**, cache behaviour, no-traceback-leak. |

Coverage is ~94–100% across the business-logic modules.

Two tests are worth singling out:

* **`test_greedy_matches_exhaustive_optimum`** — the optimality proof described in
  [section 10](#10-fuel-optimization-explained).
* **`test_one_routing_call_regardless_of_station_count`** — creates 300 stations along the
  route and asserts exactly **one** routing call is made. This is the anti-regression test
  for the project's central constraint.

Lint and format:

```bash
ruff check .
black --check .
```

Both are clean.

---

## 17. Security

The assignment does not call for authentication, so none is added. Everything else is
covered:

* **Input validation** in the serializer: type, length cap (200 chars), blank rejection,
  punctuation-only rejection, identical-endpoint rejection.
* **Request body size capped** at 16 KB (`DATA_UPLOAD_MAX_MEMORY_SIZE`), field count at 100.
* **Rate limiting** — 60 requests/min per IP by default.
* **No secrets in source.** Everything comes from the environment; `.env` is gitignored.
* **Production hardening is automatic** when `DEBUG=False`: `nosniff`, `X-Frame-Options:
  DENY`, secure session/CSRF cookies, with HSTS and SSL redirect available via env vars.
  (`manage.py check --deploy` reports warnings when running locally with `DEBUG=True` —
  that is expected; set `DJANGO_DEBUG=False` and a real `DJANGO_SECRET_KEY` for a
  deployment check.)
* **No stack traces to clients.** Unexpected exceptions are logged in full server-side and
  returned as a bare `INTERNAL_ERROR` (asserted by a test).
* **No SSRF** — no user-controlled URLs are fetched.
* **No SQL injection** — all queries go through the ORM; no raw or dynamically built SQL.
* **No command execution** and no LLM calls anywhere in the request path.
* **Cache keys are hashed**, so user input never becomes a raw key.
* The demo map escapes all API-derived values before inserting them into the DOM.

---

## 18. Known limitations

Stated honestly, because they are the difference between a demo and a production system:

1. **Station coordinates are municipality centroids, not rooftops.** The source data
   provides no rooftop-resolvable address for 96.6% of rows, so a station is placed at the
   centre of its town — typically a few miles from its true location. The corridor width
   exists to absorb this. It also means `detour_from_route_miles` is approximate.

   A consequence worth naming: because a station's position *along* the route inherits that
   error, a leg the planner computes as 500 miles could really be a few miles longer. For a
   trip where that matters, `FUEL_RESERVE_GALLONS` holds back a safety margin (default `0`,
   i.e. the full tank is usable) and every leg is then planned against the reduced range.
2. **The detour is not added to trip distance.** A station 8 miles off the route really
   costs ~16 miles of driving. The optimiser treats the route distance as authoritative
   and the detour as informational. Correcting this needs true station coordinates first —
   otherwise it would be false precision on top of centroid-level data.
3. **Corridor proximity is not road access.** A station within 10 miles as the crow flies
   might have no convenient exit. The dataset carries no access information.
4. **Prices are a static snapshot** with no timestamp, and 597 stations carry multiple
   observations that are averaged. Real prices change daily.
5. **6 stations (0.1%) have no coordinates** and are excluded from routing. They remain in
   the table, flagged, and are listed in `data/geocache/geocode_failures.csv`.
6. **A full tank is assumed at the start.** Documented in the response
   (`vehicle.starting_fuel_gallons`); the optimiser accepts any starting level, it is
   simply not exposed as a request field.
7. **The public OSRM and Nominatim endpoints are unsuitable for production load.**
8. **Alaska and Hawaii** have no drivable route to the mainland; such requests correctly
   return `NO_ROUTE_FOUND`.
9. **The plan is optimal for cost only** — it does not consider time, driver hours,
   amenities or brand loyalty.

---

## 19. Future improvements

* **Model the detour distance** in the cost function, once station coordinates are precise
  enough to justify it.
* **Self-host OSRM** — removes the dominant latency term and the rate limit.
* **Resolve a local gazetteer lookup for `"City, ST"` inputs**, dropping the common case
  to *zero* external geocoding calls (the index is already downloaded for enrichment).
* **PostGIS** with a real geography column and `ST_DWithin`, replacing the bounding-box
  prefilter, if the dataset grew by an order of magnitude.
* **Price history** with timestamps, so plans reflect current prices.
* **Async provider calls** if multi-waypoint routes are added.
* **Persist plans** for later retrieval by ID rather than only caching them.

---

## 20. Project structure

```
├── config/                      Django project (settings, urls, wsgi, asgi)
├── routes/
│   ├── models.py                FuelStation + indexes and query helpers
│   ├── admin.py                 Station admin with filters and search
│   ├── serializers.py           Request validation + response building
│   ├── views.py                 Thin API view
│   ├── exceptions.py            Domain errors + single DRF exception handler
│   ├── services/
│   │   ├── geometry.py          Haversine, resampling, grid index, projection
│   │   ├── routing.py           RoutingProvider ABC, OSRMProvider, cache decorator
│   │   ├── geocoding.py         GeocodingProvider ABC, NominatimProvider, cache
│   │   ├── station_finder.py    Bounding box + corridor selection
│   │   ├── fuel_optimizer.py    The gas-station-problem optimiser
│   │   ├── planner.py           Orchestration + call metrics
│   │   ├── cache.py             Normalisation + cache keys
│   │   └── http.py              Shared timeout/retry/logging
│   ├── management/commands/
│   │   ├── import_fuel_prices.py
│   │   ├── enrich_fuel_stations.py
│   │   └── benchmark_routes.py
│   ├── templates/routes/map.html   Leaflet demo
│   └── tests/                   160 tests, fully offline
├── data/
│   ├── fuel-prices-for-be-assessment.csv
│   └── README.md
├── .env.example
├── requirements.txt / requirements-dev.txt
├── pyproject.toml               ruff, black, pytest, coverage config
└── Dockerfile / docker-compose.yml
```
