"""Domain exceptions and the single DRF exception handler.

Every failure mode the API can produce is represented here so views stay thin
and clients always receive the same error envelope:

    {"error": {"code": "...", "message": "...", "details": {...}}}
"""

from __future__ import annotations

import logging
from typing import Any

from django.http import Http404
from rest_framework import status
from rest_framework.exceptions import APIException, MethodNotAllowed, NotFound, Throttled
from rest_framework.exceptions import ValidationError as DRFValidationError
from rest_framework.response import Response
from rest_framework.views import exception_handler as drf_exception_handler

logger = logging.getLogger(__name__)


class FuelRouteError(APIException):
    """Base class carrying a stable machine-readable ``code``."""

    status_code = status.HTTP_500_INTERNAL_SERVER_ERROR
    default_code = "INTERNAL_ERROR"
    default_detail = "An unexpected error occurred."

    def __init__(self, detail: str | None = None, details: dict[str, Any] | None = None):
        super().__init__(detail or self.default_detail)
        self.details = details or {}


class InvalidRequest(FuelRouteError):
    status_code = status.HTTP_400_BAD_REQUEST
    default_code = "INVALID_REQUEST"
    default_detail = "The request payload is invalid."


class LocationNotFound(FuelRouteError):
    status_code = status.HTTP_404_NOT_FOUND
    default_code = "LOCATION_NOT_FOUND"
    default_detail = "The supplied location could not be geocoded."


class LocationOutsideServiceArea(FuelRouteError):
    status_code = status.HTTP_400_BAD_REQUEST
    default_code = "LOCATION_OUTSIDE_SERVICE_AREA"
    default_detail = "Locations must be inside the United States."


class NoRouteFound(FuelRouteError):
    status_code = status.HTTP_422_UNPROCESSABLE_ENTITY
    default_code = "NO_ROUTE_FOUND"
    default_detail = "No drivable route exists between the supplied locations."


class NoFeasibleFuelPlan(FuelRouteError):
    status_code = status.HTTP_422_UNPROCESSABLE_ENTITY
    default_code = "NO_FEASIBLE_FUEL_PLAN"
    default_detail = (
        "No feasible fuel plan was found within the vehicle's range using the "
        "available fuel station dataset."
    )


class ProviderUnavailable(FuelRouteError):
    status_code = status.HTTP_502_BAD_GATEWAY
    default_code = "PROVIDER_UNAVAILABLE"
    default_detail = "An upstream routing or geocoding provider is unavailable."


def _envelope(code: str, message: str, details: dict[str, Any] | None = None) -> dict:
    payload: dict[str, Any] = {"error": {"code": code, "message": message}}
    if details:
        payload["error"]["details"] = details
    return payload


def api_exception_handler(exc: Exception, context: dict) -> Response | None:
    """Normalise every exception into the API's error envelope.

    Unexpected exceptions are logged with a traceback but never leak one to the
    client.
    """
    if isinstance(exc, FuelRouteError):
        return Response(
            _envelope(exc.default_code, str(exc.detail), exc.details),
            status=exc.status_code,
        )

    if isinstance(exc, DRFValidationError):
        return Response(
            _envelope(
                "INVALID_REQUEST",
                "The request payload is invalid.",
                {"fields": exc.detail},
            ),
            status=status.HTTP_400_BAD_REQUEST,
        )

    if isinstance(exc, MethodNotAllowed):
        return Response(
            _envelope("METHOD_NOT_ALLOWED", str(exc.detail)),
            status=status.HTTP_405_METHOD_NOT_ALLOWED,
        )

    if isinstance(exc, Throttled):
        return Response(
            _envelope("THROTTLED", str(exc.detail)),
            status=status.HTTP_429_TOO_MANY_REQUESTS,
        )

    if isinstance(exc, (NotFound, Http404)):
        return Response(
            _envelope("NOT_FOUND", "The requested resource does not exist."),
            status=status.HTTP_404_NOT_FOUND,
        )

    # Let DRF handle anything else it knows about (e.g. ParseError -> 400).
    response = drf_exception_handler(exc, context)
    if response is not None:
        code = getattr(exc, "default_code", "ERROR")
        response.data = _envelope(str(code).upper(), str(getattr(exc, "detail", exc)))
        return response

    logger.exception("Unhandled exception in %s", context.get("view"))
    return Response(
        _envelope("INTERNAL_ERROR", "An unexpected internal error occurred."),
        status=status.HTTP_500_INTERNAL_SERVER_ERROR,
    )
