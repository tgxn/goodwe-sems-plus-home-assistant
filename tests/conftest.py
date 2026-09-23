"""Shared fixtures for the SEMS+ tests (generic API response fixtures)."""

from __future__ import annotations

import json
from collections.abc import Generator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.sems_plus.const import (
    CONF_FALLBACK_INTERVAL,
    CONF_METADATA_INTERVAL,
    CONF_POLL_INTERVAL,
    CONF_REGION,
    CONF_STATION_ID,
    DOMAIN,
)
from custom_components.sems_plus.sems_api_v2 import SemsPlusClient

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Enable custom integrations defined in the test dir."""
    return


STATION_ID = "00000000-0000-4000-8000-000000000001"
INVERTER_SN = "INV0000000000001"
BATTERY_1_SN = "BAT0000000000001"
BATTERY_2_SN = "BAT0000000000002"
DONGLE_SN = "DNG0000000000001"

# Fixture dates are 2026-09-23 and the fixture station timezone is UTC.
CAPTURE_TIME = "2026-09-23 10:00:00+00:00"

DEVICE_FIXTURES = {
    INVERTER_SN: "inverter",
    BATTERY_1_SN: "battery_1",
    BATTERY_2_SN: "battery_2",
    DONGLE_SN: "dongle",
}


def load_fixture(name: str) -> Any:
    """Load a JSON fixture."""
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def device_fixture(kind: str) -> Any:
    """Return an async side effect serving a per-device fixture by serial."""

    async def side_effect(station_id: str, sn: str, device_type: str) -> Any:
        return load_fixture(f"{DEVICE_FIXTURES[sn]}_{kind}.json")

    return side_effect


@pytest.fixture
def config_entry() -> MockConfigEntry:
    """Return a configured SEMS+ entry."""
    return MockConfigEntry(
        domain=DOMAIN,
        title="Test Station",
        unique_id=STATION_ID,
        data={
            "username": "user@example.com",
            "password": "secret",
            CONF_REGION: "AU",
            CONF_STATION_ID: STATION_ID,
        },
        options={
            CONF_POLL_INTERVAL: 300,
            CONF_FALLBACK_INTERVAL: 120,
            CONF_METADATA_INTERVAL: 3600,
        },
    )


@pytest.fixture
def mock_client() -> Generator[dict[str, MagicMock]]:
    """Patch every SemsPlusClient network method with fixture data."""
    mocks: dict[str, MagicMock] = {
        "login": AsyncMock(return_value={"uid": "u", "token": "t"}),
        "get_stations": AsyncMock(return_value=load_fixture("stations.json")),
        "get_station_devices": AsyncMock(
            return_value=load_fixture("station_devices.json")
        ),
        "get_station_flow": AsyncMock(return_value=load_fixture("station_flow.json")),
        "get_station_statistics": AsyncMock(
            return_value=load_fixture("statistics_today.json")
        ),
        "get_station_production": AsyncMock(
            return_value=load_fixture("production.json")
        ),
        "get_station_basic_info": AsyncMock(
            return_value=load_fixture("basic_info.json")
        ),
        "get_device_information": AsyncMock(side_effect=device_fixture("information")),
        "get_device_related": AsyncMock(side_effect=device_fixture("related")),
        "get_device_telecounting": AsyncMock(
            side_effect=device_fixture("telecounting")
        ),
        "get_device_telemetry": AsyncMock(side_effect=device_fixture("telemetry")),
        "start_mqtt": MagicMock(),
        "stop_mqtt": AsyncMock(),
    }
    with patch.multiple(SemsPlusClient, **mocks):
        yield mocks
