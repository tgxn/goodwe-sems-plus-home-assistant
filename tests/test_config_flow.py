"""Tests for the SEMS+ config and options flows."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.sems_plus.const import (
    CONF_FALLBACK_INTERVAL,
    CONF_METADATA_INTERVAL,
    CONF_POLL_INTERVAL,
    CONF_STATION_ID,
    DOMAIN,
)
from custom_components.sems_plus.sems_api_v2 import SemsAuthError

from .conftest import CAPTURE_TIME, STATION_ID

pytestmark = pytest.mark.freeze_time(CAPTURE_TIME)

CREDENTIALS = {"username": "user@example.com", "password": "secret", "region": "AU"}
INTERVALS = {
    CONF_POLL_INTERVAL: 600,
    CONF_FALLBACK_INTERVAL: 60,
    CONF_METADATA_INTERVAL: 7200,
}


async def start_flow(hass: HomeAssistant) -> dict:
    """Start a user flow and submit credentials."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    return await hass.config_entries.flow.async_configure(
        result["flow_id"], CREDENTIALS
    )


async def test_full_flow(
    hass: HomeAssistant, mock_client: dict[str, MagicMock]
) -> None:
    """User → station → settings creates the entry with interval options."""
    result = await start_flow(hass)
    assert result["step_id"] == "station"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_STATION_ID: STATION_ID}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "settings"
    devices = result["description_placeholders"]["devices"]
    assert "All-in-One 1 (Inverter, Test Inverter)" in devices
    assert "Battery Rack 1 (Battery, Test Battery)" in devices
    assert "Dongle 1 (Dongle)" in devices

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], INTERVALS
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Test Station"
    assert result["data"] == {**CREDENTIALS, CONF_STATION_ID: STATION_ID}
    assert result["options"] == INTERVALS
    assert result["result"].unique_id == STATION_ID


async def test_invalid_auth(
    hass: HomeAssistant, mock_client: dict[str, MagicMock]
) -> None:
    """Rejected credentials show invalid_auth."""
    mock_client["login"].side_effect = SemsAuthError("bad password")
    result = await start_flow(hass)
    assert result["step_id"] == "user"
    assert result["errors"] == {"base": "invalid_auth"}


async def test_no_stations(
    hass: HomeAssistant, mock_client: dict[str, MagicMock]
) -> None:
    """An account without stations reports no_stations_found."""
    mock_client["get_stations"].return_value = []
    result = await start_flow(hass)
    assert result["step_id"] == "station"
    assert result["errors"] == {"base": "no_stations_found"}


async def test_all_stations_configured(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_client: dict[str, MagicMock],
) -> None:
    """Already-configured stations are not offered again."""
    config_entry.add_to_hass(hass)
    result = await start_flow(hass)
    assert result["errors"] == {"base": "all_stations_configured"}


async def test_options_flow(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_client: dict[str, MagicMock],
) -> None:
    """The options flow updates intervals and reloads the entry."""
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(config_entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], INTERVALS
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert config_entry.options == INTERVALS
    coordinator = config_entry.runtime_data.coordinator
    assert coordinator.update_interval.total_seconds() == 600
