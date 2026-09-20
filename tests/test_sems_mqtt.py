"""Tests for the SEMS MQTT-over-WebSocket listener."""

from __future__ import annotations

import asyncio
import logging
from decimal import Decimal
from unittest.mock import AsyncMock, Mock, patch

import pytest

from custom_components.sems_au.sems_mqtt import (
    SemsMqttConfig,
    SemsMqttListener,
    decode_mqtt_payload,
    normalize_mqtt_powerflow_payload,
)


def test_mqtt_config_parses_secure_websocket_url() -> None:
    """Test combining MQTT credentials with the selected region."""
    config = SemsMqttConfig.from_api(
        {
            "clientId": "client-id",
            "userName": "mqtt-user",
            "password": "mqtt-password",
        }
    )

    assert config.hostname == "netty-wss-au.iot.goodwe-power.com"
    assert config.port == 8885
    assert config.websocket_path == "/mqtt"
    assert config.websocket_origin == "https://au-semsplus.goodwe.com"
    assert config.use_tls is True


def test_mqtt_config_rejects_unsupported_region() -> None:
    """Test rejecting a region that has no endpoint configuration."""
    with pytest.raises(ValueError, match="Unsupported SEMS region"):
        SemsMqttConfig.from_api(
            {
                "clientId": "client-id",
                "userName": "mqtt-user",
                "password": "mqtt-password",
            },
            region="GLOBAL",
        )


def test_mqtt_config_rejects_missing_credentials() -> None:
    """Test rejecting incomplete MQTT credentials."""
    with pytest.raises(ValueError, match="missing required fields"):
        SemsMqttConfig.from_api(
            {
                "clientId": "client-id",
                "userName": "mqtt-user",
            }
        )


def test_decode_mqtt_payload_decodes_nested_message() -> None:
    """Test the nested JSON shape used by the website MQTT hook."""
    payload = b'{"title":"Live data","message":"{\\"sn\\":\\"GW123456789012\\",\\"power\\":42}"}'

    assert decode_mqtt_payload(payload) == {
        "title": "Live data",
        "message": {"sn": "GW123456789012", "power": 42},
    }


def test_listener_logs_redacted_payload(caplog: pytest.LogCaptureFixture) -> None:
    """Test that live payload logs redact inverter serial numbers."""
    listener = SemsMqttListener(Mock(), Mock(), "station-id")

    with caplog.at_level(logging.DEBUG):
        listener._handle_message(
            "/goodwe/second-data/station/station-id",
            b'{"sn":"GW123456789012","power":42}',
        )

    assert "GW123456789012" not in caplog.text
    assert "<GW1...012>" in caplog.text


def test_normalize_mqtt_powerflow_payload() -> None:
    """MQTT kW strings should normalize to station powerflow values in watts."""
    payload = {
        "traceId": "trace-id",
        "pSystem": "1.502",
        "soc": "82.0",
        "pConsum": "0.82196",
        "fAc": "50.02",
        "pDc": "1.502",
        "qAc": "0.007",
        "pAc": "0.82196",
        "flows": {"pSystem": ["pConsum", "pBat"]},
        "pf": "0.115",
        "pBat": "-0.68004",
        "pGrid": "0.0",
        "time": "2026-09-19 14:04:08",
        "stationId": "station-id",
    }

    assert normalize_mqtt_powerflow_payload(payload) == {
        "source": "mqtt",
        "pv": Decimal("1502.000"),
        "load": Decimal("821.96000"),
        "grid": Decimal("0.0"),
        "battery": Decimal("-680.04000"),
        "ac_power": Decimal("821.96000"),
        "dc_power": Decimal("1502.000"),
        "reactive_power": Decimal("7.000"),
        "grid_frequency": Decimal("50.02"),
        "power_factor": Decimal("0.115"),
        "soc": Decimal("82.0"),
        "system_power": Decimal("1502.000"),
        "station_id": "station-id",
        "last_live_update": "2026-09-19 14:04:08",
        "trace_id": "trace-id",
        "flows": {"pSystem": ["pConsum", "pBat"]},
    }


async def test_listener_connects_and_subscribes() -> None:
    """Test connecting with SEMS credentials and subscribing to station data."""
    hass = Mock()
    api = Mock()
    api.getMqttConfig.return_value = {
        "clientId": "client-id",
        "userName": "mqtt-user",
        "password": "mqtt-password",
    }
    hass.async_add_executor_job = AsyncMock(return_value=api.getMqttConfig.return_value)
    listener = SemsMqttListener(hass, api, "station-id")

    client = AsyncMock()
    client.__aenter__.return_value = client
    client.messages.__aiter__.return_value = []

    with patch(
        "custom_components.sems_au.sems_mqtt.aiomqtt.Client", return_value=client
    ) as client_class:
        task = asyncio.create_task(listener.async_run())
        await asyncio.sleep(0)
        listener.stop()
        await task

    client_class.assert_called_once()
    assert client_class.call_args.kwargs["hostname"] == (
        "netty-wss-au.iot.goodwe-power.com"
    )
    assert client_class.call_args.kwargs["port"] == 8885
    assert client_class.call_args.kwargs["websocket_path"] == "/mqtt"
    await_args = client.subscribe.await_args
    assert await_args.args == ("/goodwe/second-data/station/station-id",)


def test_listener_backoff_increases_exponentially() -> None:
    """Reconnect delay should back off to a cap after repeated failures."""
    listener = SemsMqttListener(Mock(), Mock(), "station-id")

    assert listener._get_backoff_delay(0) == 5
    assert listener._get_backoff_delay(1) == 10
    assert listener._get_backoff_delay(2) == 20
    assert listener._get_backoff_delay(5) == 60
    assert listener._get_backoff_delay(99) == 60
