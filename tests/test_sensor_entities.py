"""Tests for SEMS sensor entities (Home Assistant integration-style)."""

from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import patch

from homeassistant.const import CONF_PASSWORD, CONF_USERNAME, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.sems_au.const import CONF_STATION_ID, DOMAIN
from custom_components.sems_au.sensor import sensor_options_for_data

from .fixtures import (
    MOCK_GET_DATA_ACTUAL_JSON,
    MOCK_GET_DATA_POWERFLOW_ACTUAL_JSON,
)

MOCK_POWER_STATION_ID = "12345678-1234-5678-9abc-123456789abc"


@contextmanager
def _mock_no_battery_api(data: dict):
    """Mock the station detail API call."""
    with patch("custom_components.sems_au.sems_api.SemsApi.getData", return_value=data):
        yield


# Coordinator-compatible getData() result (this corresponds to SemsApi.getData() return value)
MOCK_GET_DATA_RESULT_MINIMAL = {
    "inverter": [
        {
            "invert_full": {
                "name": "Test Inverter",
                "sn": "GW0000SN000TEST1",
                "powerstation_id": MOCK_POWER_STATION_ID,
                "status": 1,
                "capacity": 3.0,
                "pac": 589,
                "etotal": 18843.2,
                "hour_total": 1234,
                "tempperature": 32.0,
                "eday": 8.9,
                "thismonthetotle": 85.7,
                "lastmonthetotle": 76.8,
                "iday": 1.96,
                "itotal": 4145.5,
            }
        }
    ],
    "kpi": {
        "currency": "EUR",
        "total_power": 18843.2,
    },
    "hasPowerflow": False,
    "hasEnergeStatisticsCharts": False,
}


async def test_sensor_state_from_coordinator(
    hass: HomeAssistant,
    enable_custom_integrations: None,
) -> None:
    """Test that the power sensor is created and has the expected state."""
    del enable_custom_integrations
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Test",
        data={
            CONF_USERNAME: "user",
            CONF_PASSWORD: "pass",
            CONF_STATION_ID: MOCK_POWER_STATION_ID,
        },
    )
    entry.add_to_hass(hass)

    with _mock_no_battery_api(MOCK_GET_DATA_RESULT_MINIMAL):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    ent_reg = er.async_get(hass)
    entity_id = ent_reg.async_get_entity_id(
        Platform.SENSOR, DOMAIN, f"{MOCK_POWER_STATION_ID}-GW0000SN000TEST1-power"
    )
    assert entity_id is not None

    state = hass.states.get(entity_id)
    assert state is not None
    assert state.state == "589"
    assert state.attributes.get("unit_of_measurement") == "W"
    assert "pac" not in state.attributes
    assert "capacity" not in state.attributes
    assert "status" not in state.attributes

    status_entity_id = ent_reg.async_get_entity_id(
        Platform.SENSOR, DOMAIN, f"{MOCK_POWER_STATION_ID}-GW0000SN000TEST1-status"
    )
    assert status_entity_id is not None

    status_state = hass.states.get(status_entity_id)
    assert status_state is not None
    assert status_state.state == "Normal"
    assert "pac" not in status_state.attributes
    assert "statusText" not in status_state.attributes


async def test_all_entities_exist(
    hass: HomeAssistant,
    enable_custom_integrations: None,
) -> None:
    """Test that all expected entities are created for the given payload."""
    del enable_custom_integrations

    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Test",
        data={
            CONF_USERNAME: "user",
            CONF_PASSWORD: "pass",
            CONF_STATION_ID: MOCK_POWER_STATION_ID,
        },
    )
    entry.add_to_hass(hass)

    with _mock_no_battery_api(MOCK_GET_DATA_ACTUAL_JSON["data"]):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    coordinator = entry.runtime_data.coordinator

    expected_sensor_unique_ids = {
        sensor.unique_id for sensor in sensor_options_for_data(coordinator.data)
    }
    expected_unique_ids = expected_sensor_unique_ids | {
        f"{MOCK_POWER_STATION_ID}:station:mqtt_connected"
    }

    ent_reg = er.async_get(hass)
    actual_unique_ids = {
        entity.unique_id
        for entity in er.async_entries_for_config_entry(ent_reg, entry.entry_id)
    }

    assert actual_unique_ids == expected_unique_ids


async def test_exact_unique_ids_single_inverter_fixture(
    hass: HomeAssistant,
    enable_custom_integrations: None,
) -> None:
    """Test the exact set of unique IDs for the single-inverter fixture."""
    del enable_custom_integrations

    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Test",
        data={
            CONF_USERNAME: "user",
            CONF_PASSWORD: "pass",
            CONF_STATION_ID: MOCK_POWER_STATION_ID,
        },
    )
    entry.add_to_hass(hass)

    with _mock_no_battery_api(MOCK_GET_DATA_ACTUAL_JSON["data"]):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    coordinator = entry.runtime_data.coordinator
    expected_unique_ids = {
        sensor.unique_id for sensor in sensor_options_for_data(coordinator.data)
    }
    expected_unique_ids.add(f"{MOCK_POWER_STATION_ID}:station:mqtt_connected")

    ent_reg = er.async_get(hass)
    actual_unique_ids = {
        entity.unique_id
        for entity in er.async_entries_for_config_entry(ent_reg, entry.entry_id)
    }

    assert actual_unique_ids == expected_unique_ids


async def test_exact_unique_ids_powerflow_fixture(
    hass: HomeAssistant,
    enable_custom_integrations: None,
) -> None:
    """Test the exact set of unique IDs for a station powerflow payload."""
    del enable_custom_integrations

    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Test",
        data={
            CONF_USERNAME: "user",
            CONF_PASSWORD: "pass",
            CONF_STATION_ID: MOCK_POWER_STATION_ID,
        },
    )
    entry.add_to_hass(hass)

    with _mock_no_battery_api(MOCK_GET_DATA_POWERFLOW_ACTUAL_JSON):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    coordinator = entry.runtime_data.coordinator
    expected_unique_ids = {
        sensor.unique_id for sensor in sensor_options_for_data(coordinator.data)
    }
    expected_unique_ids.add(f"{MOCK_POWER_STATION_ID}:station:mqtt_connected")

    ent_reg = er.async_get(hass)
    actual_unique_ids = {
        entity.unique_id
        for entity in er.async_entries_for_config_entry(ent_reg, entry.entry_id)
    }

    assert actual_unique_ids == expected_unique_ids


async def test_powerflow_values_from_api_fixture(
    hass: HomeAssistant,
    enable_custom_integrations: None,
) -> None:
    """Test station powerflow values extracted from the real API fixture."""
    del enable_custom_integrations

    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Test",
        data={
            CONF_USERNAME: "user",
            CONF_PASSWORD: "pass",
            CONF_STATION_ID: MOCK_POWER_STATION_ID,
        },
    )
    entry.add_to_hass(hass)

    with _mock_no_battery_api(MOCK_GET_DATA_POWERFLOW_ACTUAL_JSON):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    ent_reg = er.async_get(hass)

    powerflow_prefix = f"{MOCK_POWER_STATION_ID}-powerflow"
    inverter_sn = MOCK_GET_DATA_POWERFLOW_ACTUAL_JSON["inverter"][0]["invert_full"][
        "sn"
    ]
    inverter_prefix = f"{MOCK_POWER_STATION_ID}-{inverter_sn}"

    station_capacity_entity_id = ent_reg.async_get_entity_id(
        Platform.SENSOR, DOMAIN, f"{MOCK_POWER_STATION_ID}-rated-solar-capacity"
    )
    assert station_capacity_entity_id is not None
    station_capacity_state = hass.states.get(station_capacity_entity_id)
    assert station_capacity_state is not None
    assert float(station_capacity_state.state) == 10.12

    station_energy_entity_id = ent_reg.async_get_entity_id(
        Platform.SENSOR, DOMAIN, f"{MOCK_POWER_STATION_ID}-lifetime-solar-energy"
    )
    assert station_energy_entity_id is not None
    station_energy_state = hass.states.get(station_energy_entity_id)
    assert station_energy_state is not None
    assert float(station_energy_state.state) == 16851.1

    yield_rate_entity_id = ent_reg.async_get_entity_id(
        Platform.SENSOR, DOMAIN, f"{MOCK_POWER_STATION_ID}-yield-rate"
    )
    assert yield_rate_entity_id is not None
    yield_rate_state = hass.states.get(yield_rate_entity_id)
    assert yield_rate_state is not None
    assert float(yield_rate_state.state) == 5.0

    co2_entity_id = ent_reg.async_get_entity_id(
        Platform.SENSOR, DOMAIN, f"{MOCK_POWER_STATION_ID}-co2-avoided"
    )
    assert co2_entity_id is not None
    co2_state = hass.states.get(co2_entity_id)
    assert co2_state is not None
    assert float(co2_state.state) == 16.800546699999998

    backup_power_entity_id = ent_reg.async_get_entity_id(
        Platform.SENSOR, DOMAIN, f"{inverter_prefix}-pbackup"
    )
    assert backup_power_entity_id is not None
    backup_power_state = hass.states.get(backup_power_entity_id)
    assert backup_power_state is not None
    assert float(backup_power_state.state) == -2337.0

    inverter_soc_entity_id = ent_reg.async_get_entity_id(
        Platform.SENSOR, DOMAIN, f"{inverter_prefix}-soc"
    )
    assert inverter_soc_entity_id is not None
    inverter_soc_state = hass.states.get(inverter_soc_entity_id)
    assert inverter_soc_state is not None
    assert float(inverter_soc_state.state) == 45.0

    inverter_export_entity_id = ent_reg.async_get_entity_id(
        Platform.SENSOR, DOMAIN, f"{inverter_prefix}-total-sell"
    )
    assert inverter_export_entity_id is not None
    inverter_export_state = hass.states.get(inverter_export_entity_id)
    assert inverter_export_state is not None
    assert float(inverter_export_state.state) == 12901.226

    load_alias_entity_id = ent_reg.async_get_entity_id(
        Platform.SENSOR, DOMAIN, f"{powerflow_prefix}-load"
    )
    assert load_alias_entity_id is not None
    load_alias_state = hass.states.get(load_alias_entity_id)
    assert load_alias_state is not None
    assert float(load_alias_state.state) == 2337.0

    grid_entity_id = ent_reg.async_get_entity_id(
        Platform.SENSOR, DOMAIN, f"{powerflow_prefix}-grid"
    )
    assert grid_entity_id is not None
    grid_state = hass.states.get(grid_entity_id)
    assert grid_state is not None
    assert float(grid_state.state) == -2337.0

    pv_entity_id = ent_reg.async_get_entity_id(
        Platform.SENSOR, DOMAIN, f"{powerflow_prefix}-pv"
    )
    assert pv_entity_id is not None
    pv_state = hass.states.get(pv_entity_id)
    assert pv_state is not None
    assert float(pv_state.state) == 0.0

    battery_entity_id = ent_reg.async_get_entity_id(
        Platform.SENSOR, DOMAIN, f"{powerflow_prefix}-battery"
    )
    assert battery_entity_id is not None
    battery_state = hass.states.get(battery_entity_id)
    assert battery_state is not None
    assert float(battery_state.state) == 0.0

    genset_entity_id = ent_reg.async_get_entity_id(
        Platform.SENSOR, DOMAIN, f"{powerflow_prefix}-genset"
    )
    assert genset_entity_id is not None
    genset_state = hass.states.get(genset_entity_id)
    assert genset_state is not None
    assert float(genset_state.state) == 0.0

    soc_entity_id = ent_reg.async_get_entity_id(
        Platform.SENSOR, DOMAIN, f"{powerflow_prefix}-soc"
    )
    assert soc_entity_id is not None
    soc_state = hass.states.get(soc_entity_id)
    assert soc_state is not None
    assert float(soc_state.state) == 0.0

    load_status_entity_id = ent_reg.async_get_entity_id(
        Platform.SENSOR, DOMAIN, f"{powerflow_prefix}-load-status"
    )
    assert load_status_entity_id is not None
    load_status_state = hass.states.get(load_status_entity_id)
    assert load_status_state is not None
    assert int(float(load_status_state.state)) == -1

    # Verify the import sensor exists and has correct attributes
    import_entity_id = ent_reg.async_get_entity_id(
        Platform.SENSOR, DOMAIN, f"{powerflow_prefix}-import-energy"
    )
    assert import_entity_id is not None

    import_state = hass.states.get(import_entity_id)
    assert import_state is not None
    assert float(import_state.state) == 5.12
    assert import_state.attributes.get("unit_of_measurement") == "kWh"

    # Verify the export sensor exists and has correct attributes
    export_entity_id = ent_reg.async_get_entity_id(
        Platform.SENSOR, DOMAIN, f"{powerflow_prefix}-export-energy"
    )
    assert export_entity_id is not None

    export_state = hass.states.get(export_entity_id)
    assert export_state is not None
    assert float(export_state.state) == 23.22
    assert export_state.attributes.get("unit_of_measurement") == "kWh"

    # Verify the total import sensor
    total_import_entity_id = ent_reg.async_get_entity_id(
        Platform.SENSOR, DOMAIN, f"{powerflow_prefix}-import-energy-total"
    )
    assert total_import_entity_id is not None

    total_import_state = hass.states.get(total_import_entity_id)
    assert total_import_state is not None
    assert float(total_import_state.state) == 3977.33

    # Verify the total export sensor
    total_export_entity_id = ent_reg.async_get_entity_id(
        Platform.SENSOR, DOMAIN, f"{powerflow_prefix}-export-energy-total"
    )
    assert total_export_entity_id is not None

    total_export_state = hass.states.get(total_export_entity_id)
    assert total_export_state is not None
    assert float(total_export_state.state) == 12901.2

    # Verify daily load consumption sensor
    daily_load_entity_id = ent_reg.async_get_entity_id(
        Platform.SENSOR, DOMAIN, f"{powerflow_prefix}-daily-load-consumption"
    )
    assert daily_load_entity_id is not None
    daily_load_state = hass.states.get(daily_load_entity_id)
    assert daily_load_state is not None
    assert float(daily_load_state.state) == 12.2
    assert daily_load_state.attributes.get("unit_of_measurement") == "kWh"

    # Verify daily self use sensor
    daily_self_use_entity_id = ent_reg.async_get_entity_id(
        Platform.SENSOR, DOMAIN, f"{powerflow_prefix}-daily-self-use"
    )
    assert daily_self_use_entity_id is not None
    daily_self_use_state = hass.states.get(daily_self_use_entity_id)
    assert daily_self_use_state is not None
    assert float(daily_self_use_state.state) == 7.08

    # Verify total load consumption sensor
    total_load_entity_id = ent_reg.async_get_entity_id(
        Platform.SENSOR, DOMAIN, f"{powerflow_prefix}-total-load-consumption"
    )
    assert total_load_entity_id is not None
    total_load_state = hass.states.get(total_load_entity_id)
    assert total_load_state is not None
    assert float(total_load_state.state) == 7927.13

    # Verify daily self-sufficiency rate (0.5803 → 58.03%)
    daily_sufficiency_entity_id = ent_reg.async_get_entity_id(
        Platform.SENSOR, DOMAIN, f"{powerflow_prefix}-daily-self-sufficiency-rate"
    )
    assert daily_sufficiency_entity_id is not None
    daily_sufficiency_state = hass.states.get(daily_sufficiency_entity_id)
    assert daily_sufficiency_state is not None
    assert float(daily_sufficiency_state.state) == 58.03
    assert daily_sufficiency_state.attributes.get("unit_of_measurement") == "%"


def _build_powerflow_test_data(
    inverter_status: int = 1,
    inverter_pac: int = 500,
    inverter_temp: float = 32.0,
    inverter_eday: float = 8.9,
    inverter_iday: float = 1.96,
    total_power: float = 500.0,
    pv_value: str = "100(W)",
    pv_status: int = 1,
    load_value: str = "2337(W)",
    load_status: int = 1,
    grid_value: str = "2337(W)",
    grid_status: int = -1,
    battery_value: str = "0(W)",
    battery_status: int = 0,
    genset_value: str = "0(W)",
    soc: int = 50,
) -> dict:
    """Build test data for powerflow sensors with configurable values."""
    return {
        "inverter": [
            {
                "invert_full": {
                    "name": "Test Inverter",
                    "sn": "GW0000SN000TEST1",
                    "powerstation_id": MOCK_POWER_STATION_ID,
                    "status": inverter_status,
                    "capacity": 3.0,
                    "pac": inverter_pac,
                    "etotal": 18843.2,
                    "hour_total": 1234,
                    "tempperature": inverter_temp,
                    "eday": inverter_eday,
                    "thismonthetotle": 85.7,
                    "lastmonthetotle": 76.8,
                    "iday": inverter_iday,
                    "itotal": 4145.5,
                }
            }
        ],
        "kpi": {
            "currency": "EUR",
            "total_power": total_power,
        },
        "hasPowerflow": True,
        "hasEnergeStatisticsCharts": False,
        "homKit": {
            "sn": None,
            "homeKitLimit": False,
        },
        "powerflow": {
            "pv": pv_value,
            "pvStatus": pv_status,
            "load": load_value,
            "loadStatus": load_status,
            "grid": grid_value,
            "gridStatus": grid_status,
            "bettery": battery_value,
            "betteryStatus": battery_status,
            "genset": genset_value,
            "soc": soc,
        },
    }


async def test_powerflow_sensors_handle_empty_strings_at_night(
    hass: HomeAssistant,
    enable_custom_integrations: None,
) -> None:
    """Test that powerflow sensors handle empty string values without crashing.

    This simulates the scenario where sensors are first created with valid values,
    then receive empty strings when the inverter goes offline at night.
    """
    del enable_custom_integrations

    # Set up with valid powerflow data (daytime)
    initial_data = _build_powerflow_test_data()

    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Test",
        data={
            CONF_USERNAME: "user",
            CONF_PASSWORD: "pass",
            CONF_STATION_ID: MOCK_POWER_STATION_ID,
        },
    )
    entry.add_to_hass(hass)

    with _mock_no_battery_api(initial_data):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    ent_reg = er.async_get(hass)
    powerflow_prefix = f"{MOCK_POWER_STATION_ID}-powerflow"

    # Verify entities are created and have values
    load_entity_id = ent_reg.async_get_entity_id(
        Platform.SENSOR, DOMAIN, f"{powerflow_prefix}-load"
    )
    assert load_entity_id is not None
    load_state = hass.states.get(load_entity_id)
    assert load_state is not None
    assert float(load_state.state) == 2337.0

    load_alias_entity_id = ent_reg.async_get_entity_id(
        Platform.SENSOR, DOMAIN, f"{powerflow_prefix}-pv"
    )
    assert load_alias_entity_id is not None
    load_alias_state = hass.states.get(load_alias_entity_id)
    assert load_alias_state is not None
    assert float(load_alias_state.state) == 100.0

    battery_entity_id = ent_reg.async_get_entity_id(
        Platform.SENSOR, DOMAIN, f"{powerflow_prefix}-battery"
    )
    assert battery_entity_id is not None
    battery_state = hass.states.get(battery_entity_id)
    assert battery_state is not None
    assert float(battery_state.state) == 0.0

    # Simulate nighttime with empty strings - this was causing the crash
    nighttime_data = _build_powerflow_test_data(
        inverter_status=-1,  # Offline
        inverter_pac=0,
        inverter_temp=0.0,
        inverter_eday=0.0,
        inverter_iday=0.0,
        total_power=0.0,
        pv_value="",  # Empty string when offline
        pv_status=0,
        load_value="",  # Empty string when offline
        grid_value="-817(W)",
        battery_value="",  # Empty string when offline
        genset_value="",
        soc=0,
    )

    # Update coordinator data with nighttime empty strings
    coordinator = entry.runtime_data.coordinator
    with _mock_no_battery_api(nighttime_data):
        await coordinator.async_refresh()
        await hass.async_block_till_done()

    # The sensors should now be unknown (not crash) when values are empty strings
    load_state = hass.states.get(load_entity_id)
    assert load_state is not None
    assert load_state.state == "unknown"

    battery_state = hass.states.get(battery_entity_id)
    assert battery_state is not None
    assert battery_state.state == "unknown"

    # Load status sensor still has valid status values (not empty strings)
    # so it should have a numeric value
    load_status_entity_id = ent_reg.async_get_entity_id(
        Platform.SENSOR, DOMAIN, f"{powerflow_prefix}-load-status"
    )
    assert load_status_entity_id is not None
    load_status_state = hass.states.get(load_status_entity_id)
    assert load_status_state is not None
    # loadStatus=1 * gridStatus=-1 = -1
    assert load_status_state.state == "-1"
