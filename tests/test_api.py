"""Tests for the SEMS+ API client (HTTP layer faked, no network)."""

from __future__ import annotations

import asyncio
import json
from datetime import date
from typing import Any
from unittest.mock import AsyncMock

import pytest

from custom_components.sems_plus.const import SEMS_REGIONS
from custom_components.sems_plus.sems_api_v2 import (
    OutOfRetries,
    SemsApiError,
    SemsLiveData,
    SemsPlusClient,
    decode_mqtt_payload,
)

from .conftest import STATION_ID, load_fixture

GATEWAY = "https://au-gateway.semsportal.com/web/sems"


class FakeTransport:
    """Record requests and replay queued JSON responses."""

    def __init__(self, *responses: dict[str, Any] | None) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    async def __call__(
        self, method: str, url: str, headers: dict[str, str], **kwargs: Any
    ) -> dict[str, Any] | None:
        self.calls.append({"method": method, "url": url, "headers": headers, **kwargs})
        return self.responses.pop(0)


def make_client(
    *responses: dict[str, Any] | None,
) -> tuple[SemsPlusClient, FakeTransport]:
    """Return a client whose HTTP layer and login are faked."""
    client = SemsPlusClient(AsyncMock(), "user", "pass", SEMS_REGIONS["AU"])
    transport = FakeTransport(*responses)
    client._make_http_request = transport  # type: ignore[method-assign]
    client._login = AsyncMock(return_value={"uid": "u", "token": "t"})  # type: ignore[method-assign]
    return client, transport


def ok(data: Any) -> dict[str, Any]:
    """Return a successful SEMS+ response envelope."""
    return {"code": "00000", "description": "ok", "data": data}


async def test_station_statistics_request() -> None:
    """Statistics requests the day dimension over whole days."""
    client, transport = make_client(ok(load_fixture("statistics_today.json")))

    result = await client.get_station_statistics(
        STATION_ID, date(2026, 9, 1), date(2026, 9, 23)
    )

    call = transport.calls[0]
    assert call["method"] == "POST"
    assert call["url"] == f"{GATEWAY}/sems-plant/api/stations/statistics"
    assert call["json_data"]["dimension"] == "day"
    assert call["json_data"]["startTime"] == "2026-09-01 00:00:00"
    assert call["json_data"]["endTime"] == "2026-09-23 23:59:59"
    assert call["json_data"]["stationId"] == STATION_ID
    assert "proPurchaseStats" in call["json_data"]["items"]
    assert result["proPurchase"] == 1.0


async def test_station_production_and_basic_info_requests() -> None:
    """Production posts one day; basic info passes the station as a query param."""
    client, transport = make_client(
        ok(load_fixture("production.json")), ok(load_fixture("basic_info.json"))
    )

    production = await client.get_station_production(STATION_ID, date(2026, 9, 23))
    basic = await client.get_station_basic_info(STATION_ID)

    assert transport.calls[0]["url"] == f"{GATEWAY}/sems-plant/api/stations/production"
    assert transport.calls[0]["json_data"]["startTime"] == "2026-09-23 00:00:00"
    assert production["currency"] == "AUD"
    assert (
        transport.calls[1]["url"]
        == f"{GATEWAY}/sems-plant/api/portal/stations/basic/info"
    )
    assert transport.calls[1]["params"] == {"stationId": STATION_ID}
    assert transport.calls[1]["json_data"] == {}
    assert basic["batteryCapacity"] == 16.0


async def test_device_endpoints() -> None:
    """Per-device calls hit equipments/{sn}; missing data becomes an empty list."""
    client, transport = make_client(
        ok(load_fixture("inverter_telemetry.json")), {"code": "00000"}
    )

    telemetry = await client.get_device_telemetry(
        STATION_ID, "SN1", "ENERGY_STORAGE_INTEGRATED_CABINET"
    )
    telecounting = await client.get_device_telecounting(STATION_ID, "SN2", "DONGLE")

    assert (
        transport.calls[0]["url"]
        == f"{GATEWAY}/sems-plant/api/equipments/SN1/telemetry"
    )
    assert transport.calls[0]["params"] == {
        "deviceType": "ENERGY_STORAGE_INTEGRATED_CABINET",
        "pwId": STATION_ID,
    }
    assert telemetry[0]["code"] == "system"
    assert telecounting == []


async def test_auth_expiry_relogs_and_retries() -> None:
    """An expired session (None from the HTTP layer) triggers one re-login."""
    client, transport = make_client(None, ok(load_fixture("station_flow.json")))

    flow = await client.get_station_flow(STATION_ID)

    assert flow["pBat"] == 0.5
    assert len(transport.calls) == 2
    assert client._login.await_count == 2  # type: ignore[attr-defined]
    assert json.loads(transport.calls[1]["headers"]["token"])["token"] == "t"


async def test_out_of_retries() -> None:
    """Repeated auth failures raise OutOfRetries."""
    client, _ = make_client(None, None)
    with pytest.raises(OutOfRetries):
        await client.get_station_flow(STATION_ID)


async def test_pagination_stops_at_total() -> None:
    """Paged endpoints are fetched until `total` rows are collected."""
    client, transport = make_client(
        ok({"dataList": [{"id": "a"}], "total": 2}),
        ok({"dataList": [{"id": "b"}], "total": 2}),
    )

    assert await client.get_stations() == [{"id": "a"}, {"id": "b"}]
    assert [call["json_data"]["current"] for call in transport.calls] == [1, 2]


def test_live_data_from_payload() -> None:
    """A captured MQTT payload decodes into numeric values and flows."""
    raw = load_fixture("mqtt_messages.json")[0]
    live = SemsLiveData.from_payload(decode_mqtt_payload(json.dumps(raw).encode()))

    assert live is not None
    assert live.station_id == STATION_ID
    assert str(live.values["pBat"]) == raw["pBat"]
    assert "stationId" not in live.values
    assert live.flows == {"pBat": ["pConsum"]}


def test_live_data_nested_message() -> None:
    """A payload wrapped in a JSON string `msg` is unwrapped."""
    payload = json.dumps({"msg": json.dumps({"stationId": "x", "soc": "5"})})
    live = SemsLiveData.from_payload(decode_mqtt_payload(payload.encode()))
    assert live is not None
    assert live.values == {"soc": 5}


def test_mqtt_message_for_other_station_is_ignored() -> None:
    """Messages for another station never reach the handler."""
    client, _ = make_client()
    received: list[SemsLiveData] = []
    raw = json.dumps(load_fixture("mqtt_messages.json")[0]).encode()

    client._handle_mqtt_message("another-station", raw, received.append)
    client._handle_mqtt_message(STATION_ID, raw, received.append)

    assert len(received) == 1
    assert client.mqtt_last_message_at is not None
    assert client.mqtt_messages_received == 1


async def test_mqtt_lifecycle_backs_off_and_stops() -> None:
    """A failed connection attempt is counted and stop_mqtt ends the feed."""
    client, _ = make_client()
    client.enable_second_data = AsyncMock(side_effect=SemsApiError("offline"))  # type: ignore[method-assign]
    states: list[str] = []

    client.start_mqtt(STATION_ID, lambda live: None, states.append)
    await asyncio.sleep(0.05)
    await client.stop_mqtt()

    assert states[0] == "connecting"
    assert states[-1] == "disconnected"
    assert client.mqtt_connection_failures == 1
    assert client.mqtt_state == "disconnected"
