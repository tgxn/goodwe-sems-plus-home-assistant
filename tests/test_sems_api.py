"""Tests for the supported SEMS API surface."""

from __future__ import annotations

import json
from unittest.mock import Mock, patch

import pytest
import requests

from custom_components.sems_au.const import DEFAULT_SEMS_REGION, SEMS_REGIONS
from custom_components.sems_au.sems_api import (
    OutOfRetries,
    SemsApi,
    SemsRateLimitedError,
)

NEW_LOGIN_URL = SEMS_REGIONS[DEFAULT_SEMS_REGION].login_url


class TestSemsApi:
    """Test the read-only SEMS API client."""

    def setup_method(self) -> None:
        """Create an API client."""
        self.hass = Mock()
        self.api = SemsApi(self.hass, "test_user", "test_password")

    def test_init_uses_one_session_token(self) -> None:
        """The client should maintain one SEMS+ session."""
        assert self.api._token is None
        assert not hasattr(self.api, "_web_token")

    @patch("custom_components.sems_au.sems_api.requests.request")
    def test_make_http_request_success(self, mock_request) -> None:
        """Successful JSON responses should be returned."""
        response = Mock()
        response.json.return_value = {"code": 0, "data": {"test": "value"}}
        response.raise_for_status.return_value = None
        mock_request.return_value = response

        result = self.api._make_http_request(
            "https://example.test/api",
            {"Content-Type": "application/json"},
            data='{"test":"data"}',
            operation_name="test operation",
        )

        assert result == {"code": 0, "data": {"test": "value"}}
        mock_request.assert_called_once_with(
            "POST",
            "https://example.test/api",
            headers={"Content-Type": "application/json"},
            data='{"test":"data"}',
            json=None,
            timeout=30,
        )

    @patch("custom_components.sems_au.sems_api.requests.request")
    def test_make_http_request_rejects_api_error(self, mock_request) -> None:
        """Non-success API codes should fail validation."""
        response = Mock()
        response.json.return_value = {"code": 1001, "msg": "Invalid request"}
        response.raise_for_status.return_value = None
        mock_request.return_value = response

        assert (
            self.api._make_http_request(
                "https://example.test/api", {}, operation_name="test operation"
            )
            is None
        )

    @patch("custom_components.sems_au.sems_api.requests.request")
    def test_make_http_request_propagates_rate_limit(self, mock_request) -> None:
        """SEMS rate limits should reach the coordinator."""
        response = Mock()
        response.json.return_value = {"code": "GY0429", "data": None}
        response.raise_for_status.return_value = None
        mock_request.return_value = response

        with pytest.raises(SemsRateLimitedError):
            self.api._make_http_request(
                "https://example.test/api", {}, operation_name="test operation"
            )

    @patch("custom_components.sems_au.sems_api.requests.request")
    def test_make_http_request_propagates_network_error(self, mock_request) -> None:
        """Transport errors should be available to the caller."""
        mock_request.side_effect = requests.ConnectionError("Network error")

        with pytest.raises(requests.ConnectionError):
            self.api._make_http_request(
                "https://example.test/api", {}, operation_name="test operation"
            )

    def test_hash_password_for_login(self) -> None:
        """SEMS+ password encoding should remain protocol compatible."""
        assert self.api._hash_password_for_new_login("sems_test_password") == (
            "ZTJmYTRkNDJhZTk4Y2NiMTFkYzg0NWJhYWY1YWUxYzc="
        )

    @patch.object(SemsApi, "_make_http_request")
    def test_login_extracts_token(self, mock_http_request) -> None:
        """Login should normalize the token and gateway URL."""
        mock_http_request.return_value = {
            "code": "00000",
            "data": {"uid": "uid", "token": "token", "client": "semsPlusWeb"},
        }

        token = self.api.getLoginToken("user", "password")

        assert token == {
            "uid": "uid",
            "token": "token",
            "client": "semsPlusWeb",
            "api": SEMS_REGIONS[DEFAULT_SEMS_REGION].gateway_api_url,
        }

    @patch.object(SemsApi, "getLoginToken")
    def test_authenticated_context_reuses_one_token(self, mock_login) -> None:
        """Gateway and PowerStation calls should share the current session."""
        mock_login.return_value = {
            "uid": "uid",
            "token": "token",
            "client": "semsPlusWeb",
            "region": "au",
            "api": SEMS_REGIONS[DEFAULT_SEMS_REGION].gateway_api_url,
        }

        gateway_context = self.api._get_authenticated_request_context(
            "/sems-plant/api/portal/stations/page", False, "stations"
        )
        power_context = self.api._get_authenticated_request_context(
            "/v3/PowerStation/GetMonitorDetailByPowerstationId", False, "data"
        )

        assert mock_login.call_count == 1
        assert gateway_context is not None
        assert gateway_context[0].startswith(
            SEMS_REGIONS[DEFAULT_SEMS_REGION].gateway_api_url
        )
        assert power_context is not None
        assert power_context[0].startswith(
            SEMS_REGIONS[DEFAULT_SEMS_REGION].powerstation_api_url
        )

    @patch.object(SemsApi, "_make_http_request")
    def test_api_call_refreshes_expired_session(self, mock_http_request) -> None:
        """An expired session should be refreshed once."""
        self.api._token = {
            "uid": "old",
            "token": "old",
            "api": "https://api.test",
        }
        mock_http_request.side_effect = [None, {"code": 0, "data": {"ok": True}}]

        with patch.object(
            self.api,
            "getLoginToken",
            return_value={"uid": "new", "token": "new", "api": "https://api.test"},
        ) as mock_login:
            result = self.api._make_api_call("/test", operation_name="test")

        assert result == {"ok": True}
        mock_login.assert_called_once()

    def test_api_call_stops_after_retry_limit(self) -> None:
        """Calls without remaining authentication attempts should stop."""
        with pytest.raises(OutOfRetries):
            self.api._make_api_call("/test", maxTokenRetries=0)

    @patch.object(SemsApi, "_make_api_call")
    def test_get_stations_paginates(self, mock_api_call) -> None:
        """Named station discovery should collect every API page."""
        mock_api_call.side_effect = [
            {
                "dataList": [
                    {"id": "station-1", "name": "Home"},
                    {"id": "station-2", "name": "Shed"},
                ],
                "total": 3,
            },
            {
                "dataList": [{"id": "station-3", "name": "Workshop"}],
                "total": 3,
            },
        ]

        result = self.api.get_stations()

        assert [(station.station_id, station.name) for station in result] == [
            ("station-1", "Home"),
            ("station-2", "Shed"),
            ("station-3", "Workshop"),
        ]
        assert [
            json.loads(call.kwargs["data"])["current"]
            for call in mock_api_call.call_args_list
        ] == [1, 2]

    @patch.object(SemsApi, "_make_api_call")
    def test_get_stations_normalizes_rows(self, mock_api_call) -> None:
        """Station discovery should ignore invalid and duplicate records."""
        mock_api_call.return_value = {
            "dataList": [
                {"id": "station-1", "name": "  Home  "},
                {"id": "station-1", "name": "Duplicate"},
                {"id": "station-2", "name": ""},
                {"name": "Missing ID"},
            ],
            "total": 2,
        }

        result = self.api.get_stations()

        assert [(station.station_id, station.name) for station in result] == [
            ("station-1", "Home"),
            ("station-2", "Station station-2"),
        ]

    @patch.object(SemsApi, "_make_api_call", return_value={"unexpected": []})
    def test_get_stations_rejects_invalid_page(self, mock_api_call) -> None:
        """Malformed station pages should not create choices."""
        assert self.api.get_stations() == []
        mock_api_call.assert_called_once()

    @patch.object(SemsApi, "_make_api_call")
    def test_get_data(self, mock_api_call) -> None:
        """Station detail should use the selected station ID."""
        mock_api_call.return_value = {"power": 1500}

        assert self.api.getData("station-1") == {"power": 1500}
        mock_api_call.assert_called_once_with(
            "/v3/PowerStation/GetMonitorDetailByPowerstationId",
            data='{"powerStationId":"station-1"}',
            renewToken=False,
            maxTokenRetries=2,
            operation_name="getData API call",
        )

    @patch.object(SemsApi, "_make_api_call")
    def test_get_mqtt_config(self, mock_api_call) -> None:
        """MQTT configuration should come from the shared session."""
        mock_api_call.return_value = {"clientId": "client"}

        assert self.api.getMqttConfig() == {"clientId": "client"}
        mock_api_call.assert_called_once_with(
            "/sems-plant/api/second-data/config",
            method="GET",
            renewToken=False,
            maxTokenRetries=2,
            operation_name="getMqttConfig API call",
        )

    @patch.object(SemsApi, "_make_api_call")
    def test_enable_second_data(self, mock_api_call) -> None:
        """Successful second-data activation should return true."""
        mock_api_call.return_value = {"code": "00000"}

        assert self.api.enableSecondData("station-1") is True
        mock_api_call.assert_called_once_with(
            "/sems-plant/api/second-data/enable?stationId=station-1",
            method="GET",
            renewToken=False,
            maxTokenRetries=2,
            operation_name="enableSecondData API call",
            retry_on_api_error=False,
            return_raw_response=True,
        )
