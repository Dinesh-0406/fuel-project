"""Cache key construction and location normalisation.

Two things are cached, with different lifetimes:

* geocoding results, keyed by the normalised location string (stable for a long
  time -- a city does not move);
* routes and complete plans, keyed by rounded coordinates plus the tuning
  parameters that affect the result.

Keys are always hashes of normalised values, never raw user input, so a client
cannot poison or enumerate the cache with crafted strings.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata

_WHITESPACE = re.compile(r"\s+")
_PUNCTUATION = re.compile(r"[^a-z0-9, ]")

# Coordinates are rounded before they enter a cache key. 3 decimal places is
# ~110 m, far tighter than the accuracy of the station data, so this collapses
# trivially different requests onto one cached route.
COORDINATE_PRECISION = 3


def normalize_location(value: str) -> str:
    """Normalise a human-entered location for cache lookups.

    ``" New York, NY "`` and ``"new york,  ny"`` both collapse to
    ``"new york, ny"``.
    """
    text = unicodedata.normalize("NFKD", value or "")
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.strip().lower()
    text = _PUNCTUATION.sub(" ", text)
    text = text.replace(",", " , ")
    text = _WHITESPACE.sub(" ", text).strip()
    text = re.sub(r"\s*,\s*", ", ", text)
    return re.sub(r"(,\s*)+$", "", text)


def _digest(*parts: object) -> str:
    raw = "|".join(str(p) for p in parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def geocode_cache_key(location: str) -> str:
    return f"geocode:v1:{_digest(normalize_location(location))}"


def route_cache_key(start: tuple[float, float], finish: tuple[float, float], provider: str) -> str:
    """Key a route by rounded start/finish coordinates and provider."""
    p = COORDINATE_PRECISION
    return "route:v1:" + _digest(
        provider,
        round(start[0], p),
        round(start[1], p),
        round(finish[0], p),
        round(finish[1], p),
    )


def plan_cache_key(
    start: tuple[float, float],
    finish: tuple[float, float],
    *,
    corridor_miles: float,
    max_range_miles: float,
    mpg: float,
    dataset_version: str,
) -> str:
    """Key a fully-rendered plan.

    ``dataset_version`` changes whenever the station table is re-imported or
    re-enriched, so a stale plan can never outlive the data it was built from.
    """
    p = COORDINATE_PRECISION
    return "plan:v1:" + _digest(
        round(start[0], p),
        round(start[1], p),
        round(finish[0], p),
        round(finish[1], p),
        corridor_miles,
        max_range_miles,
        mpg,
        dataset_version,
    )
