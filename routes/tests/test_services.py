"""Provider abstractions: routing, geocoding, caching and failure handling.

Every external call is mocked -- the suite never touches the network.
"""

from __future__ import annotations

from unittest import mock

import pytest
import requests

from routes.exceptions import (
    LocationNotFound,
    LocationOutsideServiceArea,
    NoRouteFound,
    ProviderUnavailable,
)
from routes.services.cache import (
    geocode_cache_key,
    normalize_location,
    plan_cache_key,
    route_cache_key,
)
from routes.services.geocoding import CachedGeocoder, NominatimProvider
from routes.services.http import ProviderHTTPError, get_json
from routes.services.routing import CachedRoutingProvider, OSRMProvider

OSRM_OK = {
    "code": "Ok",
    "routes": [
        {
            "distance": 1_272_199.4,
            "duration": 53_440.7,
            "geometry": {
                "type": "LineString",
                "coordinates": [[-74.006, 40.7128], [-80.0, 41.0], [-87.6298, 41.8781]],
            },
        }
    ],
}


def fake_response(payload, status_code=200):
    response = mock.Mock()
    response.status_code = status_code
    response.ok = 200 <= status_code < 300
    response.json.return_value = payload
    return response


# ---------------------------------------------------------------------------
# Normalisation and cache keys
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("New York, NY", "new york, ny"),
        ("  new york ,  ny  ", "new york, ny"),
        ("NEW YORK, NY", "new york, ny"),
        ("New   York,NY", "new york, ny"),
    ],
)
def test_location_normalisation_collapses_equivalent_spellings(raw, expected):
    assert normalize_location(raw) == expected


def test_equivalent_spellings_share_a_geocode_cache_key():
    assert geocode_cache_key("New York, NY") == geocode_cache_key("  new york , ny ")
    assert geocode_cache_key("New York, NY") != geocode_cache_key("Newark, NJ")


def test_cache_keys_never_contain_raw_user_input():
    key = geocode_cache_key("'; DROP TABLE routes_fuelstation; --")
    assert "DROP" not in key and " " not in key


def test_route_cache_key_tolerates_insignificant_coordinate_drift():
    a = route_cache_key((40.71280, -74.00600), (41.87810, -87.62980), "OSRM")
    b = route_cache_key((40.712801, -74.006002), (41.878102, -87.629801), "OSRM")
    assert a == b


def test_plan_cache_key_changes_with_dataset_version():
    common = {
        "corridor_miles": 10.0,
        "max_range_miles": 500.0,
        "mpg": 10.0,
    }
    a = plan_cache_key((40.0, -75.0), (41.0, -87.0), dataset_version="1", **common)
    b = plan_cache_key((40.0, -75.0), (41.0, -87.0), dataset_version="2", **common)
    assert a != b


def test_plan_cache_key_changes_with_corridor_width():
    common = {"max_range_miles": 500.0, "mpg": 10.0, "dataset_version": "1"}
    a = plan_cache_key((40.0, -75.0), (41.0, -87.0), corridor_miles=10.0, **common)
    b = plan_cache_key((40.0, -75.0), (41.0, -87.0), corridor_miles=25.0, **common)
    assert a != b


# ---------------------------------------------------------------------------
# OSRM provider
# ---------------------------------------------------------------------------


def test_osrm_parses_a_successful_route():
    with mock.patch("routes.services.http.requests.get", return_value=fake_response(OSRM_OK)):
        result = OSRMProvider().route((40.7128, -74.006), (41.8781, -87.6298))

    assert result.distance_miles == pytest.approx(790.5, abs=0.1)
    assert result.duration_minutes == pytest.approx(890.7, abs=0.1)
    assert result.vertex_count == 3
    assert result.provider == "OSRM"


def test_osrm_requests_the_documented_parameters():
    with mock.patch(
        "routes.services.http.requests.get", return_value=fake_response(OSRM_OK)
    ) as get:
        OSRMProvider().route((40.7128, -74.006), (41.8781, -87.6298))

    url = get.call_args.args[0]
    params = get.call_args.kwargs["params"]
    # OSRM expects longitude,latitude order.
    assert url.endswith("/route/v1/driving/-74.006,40.7128;-87.6298,41.8781")
    assert params == {"overview": "full", "geometries": "geojson", "steps": "false"}


def test_osrm_makes_exactly_one_http_call_per_route():
    with mock.patch(
        "routes.services.http.requests.get", return_value=fake_response(OSRM_OK)
    ) as get:
        OSRMProvider().route((40.7128, -74.006), (41.8781, -87.6298))
    assert get.call_count == 1


def test_osrm_no_route_becomes_a_domain_error():
    with (
        mock.patch(
            "routes.services.http.requests.get", return_value=fake_response({"code": "NoRoute"})
        ),
        pytest.raises(NoRouteFound),
    ):
        OSRMProvider().route((40.0, -74.0), (21.3, -157.8))


@pytest.mark.parametrize(
    "payload",
    [
        {"code": "Ok", "routes": []},
        {"code": "Ok", "routes": [{"distance": 100.0, "duration": 10.0, "geometry": {}}]},
        {
            "code": "Ok",
            "routes": [
                {
                    "distance": 100.0,
                    "duration": 10.0,
                    "geometry": {"type": "LineString", "coordinates": [[-74.0, 40.0]]},
                }
            ],
        },
        {"nonsense": True},
    ],
)
def test_osrm_malformed_responses_never_leak(payload):
    with (
        mock.patch("routes.services.http.requests.get", return_value=fake_response(payload)),
        pytest.raises((ProviderUnavailable, NoRouteFound)),
    ):
        OSRMProvider().route((40.0, -74.0), (41.0, -87.0))


def test_osrm_timeout_becomes_provider_unavailable():
    with (
        mock.patch("routes.services.http.requests.get", side_effect=requests.Timeout()),
        mock.patch("routes.services.http.time.sleep"),
        pytest.raises(ProviderUnavailable),
    ):
        OSRMProvider(max_retries=1).route((40.0, -74.0), (41.0, -87.0))


def test_osrm_connection_error_becomes_provider_unavailable():
    with (
        mock.patch("routes.services.http.requests.get", side_effect=requests.ConnectionError()),
        mock.patch("routes.services.http.time.sleep"),
        pytest.raises(ProviderUnavailable),
    ):
        OSRMProvider(max_retries=0).route((40.0, -74.0), (41.0, -87.0))


def test_osrm_rejects_malformed_coordinates():
    with pytest.raises(ProviderUnavailable):
        OSRMProvider().route((999.0, -74.0), (41.0, -87.0))


# ---------------------------------------------------------------------------
# Retry policy
# ---------------------------------------------------------------------------


def test_get_json_retries_transient_failures_then_succeeds():
    responses = [fake_response({}, status_code=503), fake_response({"code": "Ok"})]
    with (
        mock.patch("routes.services.http.requests.get", side_effect=responses) as get,
        mock.patch("routes.services.http.time.sleep"),
    ):
        payload = get_json("https://example.test", max_retries=2)
    assert payload == {"code": "Ok"}
    assert get.call_count == 2


def test_get_json_retry_count_is_bounded():
    with (
        mock.patch(
            "routes.services.http.requests.get", return_value=fake_response({}, status_code=503)
        ) as get,
        mock.patch("routes.services.http.time.sleep"),
        pytest.raises(ProviderHTTPError),
    ):
        get_json("https://example.test", max_retries=2)
    assert get.call_count == 3  # the initial attempt plus two retries


def test_get_json_does_not_retry_client_errors():
    with (
        mock.patch(
            "routes.services.http.requests.get", return_value=fake_response({}, status_code=400)
        ) as get,
        pytest.raises(ProviderHTTPError),
    ):
        get_json("https://example.test", max_retries=3)
    assert get.call_count == 1


# ---------------------------------------------------------------------------
# Geocoding
# ---------------------------------------------------------------------------


def test_nominatim_parses_a_result_and_restricts_to_the_us():
    payload = [{"lat": "40.7128", "lon": "-74.0060", "display_name": "New York"}]
    with mock.patch(
        "routes.services.http.requests.get", return_value=fake_response(payload)
    ) as get:
        location = NominatimProvider().geocode("New York, NY")

    assert (location.latitude, location.longitude) == (40.7128, -74.006)
    assert get.call_args.kwargs["params"]["countrycodes"] == "us"


def test_nominatim_sends_an_identifying_user_agent():
    payload = [{"lat": "40.0", "lon": "-74.0"}]
    with mock.patch(
        "routes.services.http.requests.get", return_value=fake_response(payload)
    ) as get:
        NominatimProvider().geocode("Somewhere, NJ")
    assert get.call_args.kwargs["headers"]["User-Agent"]


def test_nominatim_empty_result_raises_location_not_found():
    with (
        mock.patch("routes.services.http.requests.get", return_value=fake_response([])),
        pytest.raises(LocationNotFound),
    ):
        NominatimProvider().geocode("Atlantis, XX")


def test_coordinates_outside_the_us_are_rejected():
    payload = [{"lat": "48.8566", "lon": "2.3522", "display_name": "Paris"}]
    with (
        mock.patch("routes.services.http.requests.get", return_value=fake_response(payload)),
        pytest.raises(LocationOutsideServiceArea),
    ):
        NominatimProvider().geocode("Paris")


# ---------------------------------------------------------------------------
# Caching behaviour
# ---------------------------------------------------------------------------


def test_geocoder_cache_prevents_a_second_upstream_call():
    payload = [{"lat": "40.7128", "lon": "-74.0060"}]
    geocoder = CachedGeocoder(NominatimProvider())
    with mock.patch(
        "routes.services.http.requests.get", return_value=fake_response(payload)
    ) as get:
        geocoder.geocode("New York, NY")
        geocoder.geocode("  new york , ny ")  # same place, different spelling

    assert get.call_count == 1
    assert geocoder.call_count == 1


def test_geocoder_caches_negative_results():
    geocoder = CachedGeocoder(NominatimProvider())
    with mock.patch("routes.services.http.requests.get", return_value=fake_response([])) as get:
        for _ in range(3):
            with pytest.raises(LocationNotFound):
                geocoder.geocode("Nowhere, ZZ")
    assert get.call_count == 1


def test_route_cache_prevents_a_second_routing_call():
    router = CachedRoutingProvider(OSRMProvider())
    with mock.patch(
        "routes.services.http.requests.get", return_value=fake_response(OSRM_OK)
    ) as get:
        first = router.route((40.7128, -74.006), (41.8781, -87.6298))
        assert router.last_call_was_cached is False
        second = router.route((40.7128, -74.006), (41.8781, -87.6298))
        assert router.last_call_was_cached is True

    assert get.call_count == 1
    assert first.distance_miles == second.distance_miles
    assert first.coordinates == second.coordinates


def test_route_cache_distinguishes_different_journeys():
    router = CachedRoutingProvider(OSRMProvider())
    with mock.patch(
        "routes.services.http.requests.get", return_value=fake_response(OSRM_OK)
    ) as get:
        router.route((40.7128, -74.006), (41.8781, -87.6298))
        router.route((34.0522, -118.2437), (41.8781, -87.6298))
    assert get.call_count == 2


def test_cached_route_survives_a_cold_provider():
    """Once cached, a route is served even if the provider then fails."""
    router = CachedRoutingProvider(OSRMProvider())
    with mock.patch("routes.services.http.requests.get", return_value=fake_response(OSRM_OK)):
        router.route((40.7128, -74.006), (41.8781, -87.6298))

    with mock.patch("routes.services.http.requests.get", side_effect=requests.Timeout()):
        result = router.route((40.7128, -74.006), (41.8781, -87.6298))
    assert result.distance_miles == pytest.approx(790.5, abs=0.1)


def test_osrm_http_400_no_route_is_a_routing_outcome_not_an_outage():
    """OSRM reports an impossible route as HTTP 400 with code "NoRoute".

    Honolulu -> Denver really does this. It must surface as 422 NO_ROUTE_FOUND,
    not as a 502 blaming the provider.
    """
    body = {"message": "Impossible route between points", "code": "NoRoute"}
    with (
        mock.patch(
            "routes.services.http.requests.get", return_value=fake_response(body, status_code=400)
        ),
        pytest.raises(NoRouteFound),
    ):
        OSRMProvider().route((21.3069, -157.8583), (39.7392, -104.9903))


def test_osrm_http_400_is_not_retried():
    body = {"code": "NoRoute"}
    with (
        mock.patch(
            "routes.services.http.requests.get", return_value=fake_response(body, status_code=400)
        ) as get,
        pytest.raises(NoRouteFound),
    ):
        OSRMProvider(max_retries=3).route((21.3069, -157.8583), (39.7392, -104.9903))
    assert get.call_count == 1


def test_get_json_still_raises_on_unexpected_client_errors():
    """Only statuses the caller opts into are treated as payloads."""
    with (
        mock.patch(
            "routes.services.http.requests.get", return_value=fake_response({}, status_code=404)
        ),
        pytest.raises(ProviderHTTPError),
    ):
        get_json("https://example.test", payload_statuses={400})


def test_geocoder_client_errors_are_not_swallowed():
    """Nominatim's 403 for a placeholder User-Agent must stay a provider error."""
    with (
        mock.patch(
            "routes.services.http.requests.get", return_value=fake_response({}, status_code=403)
        ),
        pytest.raises(ProviderUnavailable),
    ):
        NominatimProvider().geocode("New York, NY")
