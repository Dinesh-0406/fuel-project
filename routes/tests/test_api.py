"""End-to-end API behaviour with every external provider mocked."""

from __future__ import annotations

from decimal import Decimal
from unittest import mock

import pytest
import requests
from rest_framework.test import APIClient

from routes.models import FuelStation
from routes.tests.conftest import straight_line

pytestmark = pytest.mark.django_db

URL = "/api/v1/routes/"

# A ~700-mile north-south route used by most tests.
ROUTE_COORDINATES = straight_line((35.0, -90.0), (45.0, -90.0), points=400)
ROUTE_DISTANCE_METERS = 700 * 1609.344

GEOCODES = {
    "memphis, tn": (35.0, -90.0),
    "duluth, mn": (45.0, -90.0),
}


def osrm_payload(distance_meters=ROUTE_DISTANCE_METERS, coordinates=None):
    return {
        "code": "Ok",
        "routes": [
            {
                "distance": distance_meters,
                "duration": distance_meters / 26.8,  # ~60 mph
                "geometry": {
                    "type": "LineString",
                    "coordinates": [list(c) for c in (coordinates or ROUTE_COORDINATES)],
                },
            }
        ],
    }


def fake_get(url, params=None, headers=None, timeout=None):
    """Single stub standing in for both Nominatim and OSRM."""
    response = mock.Mock(status_code=200, ok=True)
    if "/search" in url:
        query = (params or {}).get("q", "")
        coordinates = GEOCODES.get(query)
        response.json.return_value = (
            [{"lat": str(coordinates[0]), "lon": str(coordinates[1]), "display_name": query}]
            if coordinates
            else []
        )
    else:
        response.json.return_value = osrm_payload()
    return response


@pytest.fixture
def client() -> APIClient:
    return APIClient()


@pytest.fixture
def stations_along_route(make_station):
    """Stations roughly every 140 miles down the 90W meridian."""
    created = []
    for index, (latitude, price) in enumerate(
        [(43.0, "3.90"), (41.0, "3.20"), (39.0, "3.60"), (37.0, "3.10")]
    ):
        created.append(
            make_station(
                opis_id=f"s{index}",
                truckstop_name=f"Truck Stop {index}",
                city=f"City {index}",
                state="MO",
                latitude=latitude,
                longitude=-90.0,
                retail_price=Decimal(price),
            )
        )
    return created


@pytest.fixture
def patched_providers():
    with mock.patch("routes.services.http.requests.get", side_effect=fake_get) as get:
        yield get


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"start": "Memphis, TN"},
        {"finish": "Duluth, MN"},
        {"start": "", "finish": "Duluth, MN"},
        {"start": "Memphis, TN", "finish": ""},
        {"start": "   ", "finish": "Duluth, MN"},
        {"start": "!!!", "finish": "Duluth, MN"},
        {"start": "x" * 500, "finish": "Duluth, MN"},
    ],
)
def test_invalid_payloads_are_rejected(client, payload):
    response = client.post(URL, payload, format="json")
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_REQUEST"


def test_identical_start_and_finish_is_rejected(client):
    response = client.post(
        URL, {"start": "New York, NY", "finish": " new york , ny "}, format="json"
    )
    assert response.status_code == 400
    assert "finish" in response.json()["error"]["details"]["fields"]


def test_malformed_json_is_rejected(client):
    response = client.post(URL, "{not json", content_type="application/json")
    assert response.status_code == 400
    assert "error" in response.json()


@pytest.mark.parametrize("method", ["get", "put", "patch", "delete"])
def test_unsupported_methods_are_rejected(client, method):
    response = getattr(client, method)(URL)
    assert response.status_code == 405
    assert response.json()["error"]["code"] == "METHOD_NOT_ALLOWED"


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_successful_plan_has_the_documented_shape(client, stations_along_route, patched_providers):
    response = client.post(URL, {"start": "Memphis, TN", "finish": "Duluth, MN"}, format="json")
    assert response.status_code == 200
    body = response.json()

    assert set(body) == {"start", "finish", "route", "vehicle", "fuel_plan", "meta"}
    assert body["start"]["latitude"] == 35.0
    assert body["finish"]["longitude"] == -90.0

    assert body["route"]["distance_miles"] == pytest.approx(700.0, abs=0.5)
    assert body["route"]["geometry"]["type"] == "LineString"
    assert len(body["route"]["geometry"]["coordinates"]) == len(ROUTE_COORDINATES)

    assert body["vehicle"] == {
        "max_range_miles": 500.0,
        "fuel_efficiency_mpg": 10.0,
        "tank_capacity_gallons": 50.0,
        "starting_fuel_gallons": 50.0,
    }


def test_route_geometry_is_map_ready_geojson(client, stations_along_route, patched_providers):
    body = client.post(URL, {"start": "Memphis, TN", "finish": "Duluth, MN"}, format="json").json()
    coordinates = body["route"]["geometry"]["coordinates"]

    assert all(len(point) == 2 for point in coordinates)
    # GeoJSON is [longitude, latitude]; this route runs up the 90W meridian.
    assert all(point[0] == pytest.approx(-90.0) for point in coordinates)
    assert coordinates[0][1] == pytest.approx(35.0)
    assert coordinates[-1][1] == pytest.approx(45.0)


def test_a_700_mile_trip_produces_at_least_one_stop(
    client, stations_along_route, patched_providers
):
    body = client.post(URL, {"start": "Memphis, TN", "finish": "Duluth, MN"}, format="json").json()
    plan = body["fuel_plan"]

    assert plan["stop_count"] >= 1
    assert Decimal(plan["total_cost"]) > 0
    for stop in plan["stops"]:
        assert stop["station"]["latitude"] is not None
        assert Decimal(stop["cost"]) >= 0
        assert stop["fuel_after_purchase_gallons"] <= 50.0


def test_money_is_serialised_as_fixed_precision_strings(
    client, stations_along_route, patched_providers
):
    plan = client.post(URL, {"start": "Memphis, TN", "finish": "Duluth, MN"}, format="json").json()[
        "fuel_plan"
    ]

    assert isinstance(plan["total_cost"], str)
    assert plan["total_cost"].count(".") == 1
    assert len(plan["total_cost"].split(".")[1]) == 2
    for stop in plan["stops"]:
        assert isinstance(stop["cost"], str)
        assert len(stop["cost"].split(".")[1]) == 2


def test_total_cost_equals_the_sum_of_the_stops(client, stations_along_route, patched_providers):
    plan = client.post(URL, {"start": "Memphis, TN", "finish": "Duluth, MN"}, format="json").json()[
        "fuel_plan"
    ]

    total = sum(Decimal(stop["cost"]) for stop in plan["stops"])
    assert Decimal(plan["total_cost"]) == total


def test_stops_are_ordered_along_the_route(client, make_station, patched_providers):
    for index, latitude in enumerate([43.0, 41.0, 39.0, 37.0]):
        make_station(
            opis_id=f"m{index}",
            latitude=latitude,
            longitude=-90.0,
            retail_price=Decimal("3.00"),
            state="MO",
        )
    plan = client.post(URL, {"start": "Memphis, TN", "finish": "Duluth, MN"}, format="json").json()[
        "fuel_plan"
    ]

    distances = [s["distance_from_start_miles"] for s in plan["stops"]]
    assert distances == sorted(distances)
    assert [s["sequence"] for s in plan["stops"]] == list(range(1, len(distances) + 1))


def test_short_trip_needs_no_fuel_stops(client, stations_along_route, patched_providers):
    short = osrm_payload(
        distance_meters=200 * 1609.344,
        coordinates=straight_line((35.0, -90.0), (38.0, -90.0), points=100),
    )

    def stub(url, params=None, headers=None, timeout=None):
        if "/search" in url:
            return fake_get(url, params, headers, timeout)
        return mock.Mock(status_code=200, ok=True, **{"json.return_value": short})

    with mock.patch("routes.services.http.requests.get", side_effect=stub):
        body = client.post(
            URL, {"start": "Memphis, TN", "finish": "Duluth, MN"}, format="json"
        ).json()

    assert body["fuel_plan"]["stop_count"] == 0
    assert body["fuel_plan"]["total_cost"] == "0.00"


# ---------------------------------------------------------------------------
# External call budget and caching
# ---------------------------------------------------------------------------


def test_one_routing_call_regardless_of_station_count(client, make_station):
    """The station count must not influence how many routing calls are made."""
    for index in range(300):
        make_station(
            opis_id=f"bulk{index}",
            latitude=35.0 + index * 0.03,
            longitude=-90.0,
            retail_price=Decimal("3.50"),
            state="MO",
        )

    with mock.patch("routes.services.http.requests.get", side_effect=fake_get) as get:
        body = client.post(
            URL, {"start": "Memphis, TN", "finish": "Duluth, MN"}, format="json"
        ).json()

    routing_calls = [c for c in get.call_args_list if "/search" not in c.args[0]]
    assert len(routing_calls) == 1
    assert body["meta"]["external_calls"] == {"geocoding": 2, "routing": 1}
    assert body["meta"]["fuel_station_count_considered"] > 100


def test_repeated_request_makes_no_external_calls(client, stations_along_route, patched_providers):
    payload = {"start": "Memphis, TN", "finish": "Duluth, MN"}
    first = client.post(URL, payload, format="json").json()
    calls_after_first = patched_providers.call_count

    second = client.post(URL, payload, format="json").json()

    assert patched_providers.call_count == calls_after_first  # nothing new
    assert first["meta"]["plan_cache_hit"] is False
    assert second["meta"]["plan_cache_hit"] is True
    assert second["meta"]["external_calls"] == {"geocoding": 0, "routing": 0}
    assert second["fuel_plan"]["total_cost"] == first["fuel_plan"]["total_cost"]
    # A cache hit reports its own timing, and spends no time on the provider.
    assert second["meta"]["timing_ms"]["routing_provider"] == 0.0
    assert second["meta"]["timing_ms"]["total"] < first["meta"]["timing_ms"]["total"]


def test_differently_spelled_requests_share_one_cached_plan(
    client, stations_along_route, patched_providers
):
    client.post(URL, {"start": "Memphis, TN", "finish": "Duluth, MN"}, format="json")
    calls = patched_providers.call_count

    response = client.post(
        URL, {"start": "  memphis ,  tn ", "finish": "DULUTH, MN"}, format="json"
    )

    assert patched_providers.call_count == calls
    body = response.json()
    assert body["meta"]["plan_cache_hit"] is True
    # The caller's own spelling is echoed back, not the cached one.
    assert body["start"]["input"] == "memphis ,  tn"


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


def test_unknown_location_returns_404(client, patched_providers):
    response = client.post(URL, {"start": "Atlantis, ZZ", "finish": "Duluth, MN"}, format="json")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "LOCATION_NOT_FOUND"


def test_no_stations_returns_422_with_a_clear_code(client, patched_providers):
    assert FuelStation.objects.count() == 0
    response = client.post(URL, {"start": "Memphis, TN", "finish": "Duluth, MN"}, format="json")
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "NO_FEASIBLE_FUEL_PLAN"


def test_unreachable_station_gap_returns_422(client, make_station, patched_providers):
    """Only a station 40 miles in: the remaining 660 miles cannot be covered."""
    make_station(opis_id="near-start", latitude=35.5, longitude=-90.0, state="MO")

    response = client.post(URL, {"start": "Memphis, TN", "finish": "Duluth, MN"}, format="json")
    assert response.status_code == 422
    body = response.json()["error"]
    assert body["code"] == "NO_FEASIBLE_FUEL_PLAN"
    assert "details" in body


def test_routing_provider_failure_returns_502(client, stations_along_route):
    def stub(url, params=None, headers=None, timeout=None):
        if "/search" in url:
            return fake_get(url, params, headers, timeout)
        raise requests.ConnectionError("boom")

    with (
        mock.patch("routes.services.http.requests.get", side_effect=stub),
        mock.patch("routes.services.http.time.sleep"),
    ):
        response = client.post(URL, {"start": "Memphis, TN", "finish": "Duluth, MN"}, format="json")

    assert response.status_code == 502
    assert response.json()["error"]["code"] == "PROVIDER_UNAVAILABLE"


def test_geocoder_failure_returns_502(client, stations_along_route):
    with (
        mock.patch("routes.services.http.requests.get", side_effect=requests.Timeout()),
        mock.patch("routes.services.http.time.sleep"),
    ):
        response = client.post(URL, {"start": "Memphis, TN", "finish": "Duluth, MN"}, format="json")
    assert response.status_code == 502


def test_no_route_returns_422(client, stations_along_route):
    def stub(url, params=None, headers=None, timeout=None):
        if "/search" in url:
            return fake_get(url, params, headers, timeout)
        return mock.Mock(status_code=200, ok=True, **{"json.return_value": {"code": "NoRoute"}})

    with mock.patch("routes.services.http.requests.get", side_effect=stub):
        response = client.post(URL, {"start": "Memphis, TN", "finish": "Duluth, MN"}, format="json")
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "NO_ROUTE_FOUND"


def test_errors_never_leak_a_traceback(client, stations_along_route):
    with (
        mock.patch(
            "routes.services.planner.RoutePlanner.plan_for_endpoints",
            side_effect=RuntimeError("internal detail: secret"),
        ),
        mock.patch("routes.services.http.requests.get", side_effect=fake_get),
    ):
        response = client.post(URL, {"start": "Memphis, TN", "finish": "Duluth, MN"}, format="json")

    assert response.status_code == 500
    body = response.json()
    assert body["error"]["code"] == "INTERNAL_ERROR"
    assert "secret" not in str(body)
    assert "Traceback" not in str(body)
