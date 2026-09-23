"""Tests for SEMS+ setup, coordinator task runners and live updates."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.sems_plus.const import DOMAIN

from .conftest import BATTERY_1_SN, CAPTURE_TIME, STATION_ID, load_fixture

pytestmark = pytest.mark.freeze_time(CAPTURE_TIME)


async def setup_entry(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """Add and set up the entry."""
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


def state(hass: HomeAssistant, entity_id: str) -> str:
    """Return an entity's state, failing clearly if it does not exist."""
    current = hass.states.get(entity_id)
    assert current is not None, f"{entity_id} missing"
    return current.state


async def test_setup_creates_devices_and_values(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_client: dict[str, MagicMock],
) -> None:
    """Setup creates the station, inverter, racks and dongle with mapped values."""
    await setup_entry(hass, config_entry)
    assert config_entry.state is ConfigEntryState.LOADED

    registry = dr.async_get(hass)
    station = registry.async_get_device_by_identifier(
        (DOMAIN, STATION_ID), config_entry.entry_id
    )
    rack = registry.async_get_device_by_identifier(
        (DOMAIN, BATTERY_1_SN), config_entry.entry_id
    )
    assert station is not None and station.name == "Test Station"
    assert rack is not None and rack.name == "Battery Rack 1"
    assert rack.via_device_id == station.id
    assert rack.model == "Test Battery"
    assert rack.sw_version == "BMS 1.0, DCDC 1.0"
    assert len(dr.async_entries_for_config_entry(registry, config_entry.entry_id)) == 5

    # Live values (REST flow before MQTT connects): discharging 500 W.
    assert state(hass, "sensor.test_station_battery_power") == "500"
    assert state(hass, "sensor.test_station_battery_discharging_power") == "500"
    assert state(hass, "sensor.test_station_battery_charging_power") == "0"
    assert state(hass, "sensor.test_station_battery_state") == "discharging"
    assert state(hass, "sensor.test_station_grid_state") == "idle"
    assert state(hass, "sensor.test_station_battery_state_of_charge") == "50"

    # Daily totals from stations/statistics.
    assert state(hass, "sensor.test_station_solar_energy_today") == "10.0"
    assert state(hass, "sensor.test_station_grid_import_today") == "1.0"
    assert state(hass, "sensor.test_station_grid_export_today") == "3.0"
    assert state(hass, "sensor.test_station_home_consumption_today") == "8.0"
    assert float(state(hass, "sensor.test_station_self_sufficiency_today")) == (
        pytest.approx(87.5)
    )
    income = hass.states.get("sensor.test_station_income_today")
    assert income is not None and income.attributes["unit_of_measurement"] == "AUD"

    # Station metadata, status and aggregates.
    status = hass.states.get("sensor.test_station_status")
    assert status is not None and status.state == "working"
    assert status.attributes["code"] == 1
    assert status.attributes["source"] == "station"
    assert state(hass, "sensor.test_station_battery_capacity") == "16.0"
    assert state(hass, "sensor.test_station_battery_state_of_health") == "99"

    # Per-device values.
    assert state(hass, "sensor.battery_rack_1_state_of_charge") == "50"
    assert state(hass, "sensor.battery_rack_1_power") == "250"
    assert state(hass, "sensor.battery_rack_1_max_cell_voltage") == "3.3"
    assert state(hass, "sensor.all_in_one_1_grid_voltage") == "240"
    assert state(hass, "sensor.all_in_one_1_pv_string_2_voltage") == "200"
    assert state(hass, "sensor.all_in_one_1_status") == "running"
    assert state(hass, "binary_sensor.all_in_one_1_on_grid") == "on"
    assert state(hass, "sensor.dongle_1_status") == "online"

    mock_client["start_mqtt"].assert_called_once()


async def test_live_update_does_not_reschedule_poll(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_client: dict[str, MagicMock],
) -> None:
    """MQTT messages reach entities without resetting the REST poll timer."""
    await setup_entry(hass, config_entry)
    coordinator = config_entry.runtime_data.coordinator
    client = config_entry.runtime_data.client
    unsub_before = coordinator._unsub_refresh
    assert state(hass, "sensor.test_station_last_live_feed_message") == "unknown"

    raw = dict(load_fixture("mqtt_messages.json")[0])
    raw.update({"pBat": "1.5", "flows": {"pSystem": ["pBat"]}})
    client._handle_mqtt_message(
        STATION_ID, json.dumps(raw).encode(), coordinator.async_handle_live
    )
    await hass.async_block_till_done()

    assert state(hass, "sensor.test_station_battery_power") == "-1500"
    assert state(hass, "sensor.test_station_battery_charging_power") == "1500"
    assert state(hass, "sensor.test_station_battery_state") == "charging"
    assert state(hass, "sensor.battery_rack_1_power") == "-250"
    assert state(hass, "sensor.test_station_last_live_feed_message") == (
        dt_util.utcnow().isoformat(timespec="seconds")
    )
    assert coordinator._unsub_refresh is unsub_before


async def test_live_feed_health_entities(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_client: dict[str, MagicMock],
) -> None:
    """Feed state and failures come from the client; the fallback follows them."""
    await setup_entry(hass, config_entry)
    coordinator = config_entry.runtime_data.coordinator
    client = config_entry.runtime_data.client
    client._mqtt_status_handler = coordinator.async_handle_mqtt_state
    assert state(hass, "binary_sensor.test_station_live_feed") == "off"

    client._set_mqtt_state("connecting")
    client._mqtt_failures = 2
    client._set_mqtt_state("connecting")  # same state, new failure count
    await hass.async_block_till_done()
    assert coordinator._fallback_unsub is not None
    assert state(hass, "sensor.test_station_live_feed_state") == "connecting"
    assert state(hass, "sensor.test_station_live_feed_connection_failures") == "2"

    client._mqtt_failures = 0
    client._set_mqtt_state("connected")
    await hass.async_block_till_done()
    assert coordinator._fallback_unsub is None
    assert state(hass, "binary_sensor.test_station_live_feed") == "on"
    assert state(hass, "sensor.test_station_live_feed_connection_failures") == "0"


async def test_unload_stops_live_feed(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_client: dict[str, MagicMock],
) -> None:
    """Unloading stops the MQTT feed."""
    await setup_entry(hass, config_entry)
    assert await hass.config_entries.async_unload(config_entry.entry_id)
    await hass.async_block_till_done()

    assert config_entry.state is ConfigEntryState.NOT_LOADED
    mock_client["stop_mqtt"].assert_awaited_once()
