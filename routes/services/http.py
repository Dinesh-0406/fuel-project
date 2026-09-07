"""Shared HTTP client behaviour for outbound provider calls.

Centralised so every provider gets the same timeout handling, bounded retries
and latency logging, and so tests have a single place to patch.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import requests

logger = logging.getLogger(__name__)

# Transient conditions worth one more attempt on an idempotent GET.
RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})


class ProviderHTTPError(Exception):
    """Any failure talking to an upstream provider."""


def get_json(
    url: str,
    *,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 10.0,
    max_retries: int = 2,
    backoff_seconds: float = 0.5,
    provider: str = "provider",
) -> dict | list:
    """GET a JSON document with bounded retries.

    Returns whatever the endpoint yields -- an object or an array (Nominatim's
    /search returns the latter).

    Only GETs are retried -- they are idempotent, so a repeat is always safe.
    Retries are capped and use linear backoff, so there is no unbounded loop.
    """
    last_error: str = "unknown error"
    attempts = max(1, max_retries + 1)

    for attempt in range(1, attempts + 1):
        started = time.perf_counter()
        try:
            response = requests.get(url, params=params, headers=headers, timeout=timeout)
            elapsed_ms = (time.perf_counter() - started) * 1000.0

            if response.status_code in RETRYABLE_STATUS:
                last_error = f"HTTP {response.status_code}"
                logger.warning(
                    "%s returned %s in %.0fms (attempt %d/%d)",
                    provider,
                    response.status_code,
                    elapsed_ms,
                    attempt,
                    attempts,
                )
            elif not response.ok:
                # 4xx other than 429 will not improve on retry.
                raise ProviderHTTPError(f"{provider} returned HTTP {response.status_code}")
            else:
                logger.info("%s responded in %.0fms", provider, elapsed_ms)
                try:
                    return response.json()
                except ValueError as exc:
                    raise ProviderHTTPError(
                        f"{provider} returned a malformed JSON response"
                    ) from exc

        except requests.Timeout:
            last_error = "timed out"
            logger.warning(
                "%s timed out after %.1fs (attempt %d/%d)", provider, timeout, attempt, attempts
            )
        except requests.RequestException as exc:
            last_error = f"connection error: {exc.__class__.__name__}"
            logger.warning(
                "%s connection error %s (attempt %d/%d)",
                provider,
                exc.__class__.__name__,
                attempt,
                attempts,
            )

        if attempt < attempts:
            time.sleep(backoff_seconds * attempt)

    raise ProviderHTTPError(f"{provider} unavailable ({last_error})")
