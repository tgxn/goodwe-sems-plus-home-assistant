"""The sems integration."""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    CONF_REGION,
    CONF_STATION_ID,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_SEMS_REGION,
    DOMAIN,
    GOODWE_SPELLING,
    PLATFORMS,
    redact_for_log,
)
from .sems_api import SemsApi, SemsRateLimitedError
from .sems_mqtt import SemsMqttListener

_LOGGER: logging.Logger = logging.getLogger(__package__)

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

_NUMBER_PATTERN = re.compile(r"-?\d+(?:\.\d+)?")


def _decimal_value(value: Any) -> Decimal | None:
    """Return a Decimal from SEMS numeric strings, including values with units."""

    if value in (None, ""):
        return None
    if isinstance(value, str):
        match = _NUMBER_PATTERN.search(value)
        if match is None:
            return None
        value = match.group(0)
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _int_value(value: Any) -> int | None:
    """Return an int for SEMS status values."""

    decimal_value = _decimal_value(value)
    if decimal_value is None:
        return None
    return int(decimal_value)


def _set_if_not_none(data: dict[str, Any], key: str, value: Any) -> None:
    """Set a key only when SEMS supplied a usable value."""

    if value is not None:
        data[key] = value


def _normalize_rest_powerflow(
    data_result: dict[str, Any], kpi: dict[str, Any]
) -> dict[str, Any] | None:
    """Normalize REST powerflow data to the station powerflow model."""

    if not data_result.get("hasPowerflow"):
        return None

    raw_powerflow = data_result.get("powerflow")
    if not isinstance(raw_powerflow, dict):
        raw_powerflow = {}

    powerflow: dict[str, Any] = {"source": "rest"}
    for source_key, target_key in (
        ("pv", "pv"),
        ("load", "load"),
        ("grid", "grid"),
        (GOODWE_SPELLING.battery, "battery"),
        ("genset", "genset"),
    ):
        _set_if_not_none(
            powerflow, target_key, _decimal_value(raw_powerflow.get(source_key))
        )

    for source_key, target_key in (
        ("pvStatus", "pvStatus"),
        ("loadStatus", "loadStatus"),
        ("gridStatus", "gridStatus"),
        (GOODWE_SPELLING.batteryStatus, "batteryStatus"),
    ):
        _set_if_not_none(
            powerflow, target_key, _int_value(raw_powerflow.get(source_key))
        )

    _set_if_not_none(powerflow, "soc", _decimal_value(raw_powerflow.get("soc")))
    _set_if_not_none(
        powerflow, "all_time_generation", _decimal_value(kpi.get("total_power"))
    )

    has_energy_statistics_charts = bool(
        data_result.get(GOODWE_SPELLING.hasEnergyStatisticsCharts)
    )
    powerflow[GOODWE_SPELLING.hasEnergyStatisticsCharts] = has_energy_statistics_charts

    if has_energy_statistics_charts:
        charts = data_result.get(GOODWE_SPELLING.energyStatisticsCharts)
        if not isinstance(charts, dict):
            charts = {}
        totals = data_result.get(GOODWE_SPELLING.energyStatisticsTotals)
        if not isinstance(totals, dict):
            totals = {}

        for key, value in charts.items():
            _set_if_not_none(powerflow, f"Charts_{key}", _decimal_value(value))
        for key, value in totals.items():
            _set_if_not_none(powerflow, f"Totals_{key}", _decimal_value(value))

    return powerflow


def _normalize_station_data(
    data_result: dict[str, Any], kpi: dict[str, Any]
) -> dict[str, Any]:
    """Normalize station-level REST values."""

    station: dict[str, Any] = {}
    info = data_result.get("info")
    if not isinstance(info, dict):
        info = {}

    for source_key, target_key in (
        ("capacity", "rated_solar_capacity"),
        ("battery_capacity", "rated_battery_capacity"),
        ("longitude", "longitude"),
        ("latitude", "latitude"),
        ("time_span", "timezone_offset"),
    ):
        _set_if_not_none(station, target_key, _decimal_value(info.get(source_key)))

    _set_if_not_none(station, "status", _int_value(info.get("status")))
    powerstation_type = info.get("powerstation_type")
    if isinstance(powerstation_type, str) and powerstation_type:
        station["powerstation_type"] = powerstation_type

    for source_key, target_key in (
        ("month_generation", "energy_this_month"),
        ("pac", "current_output_power"),
        ("total_power", "lifetime_solar_energy"),
        ("day_income", "income_today"),
        ("total_income", "income_total"),
        ("yield_rate", "yield_rate"),
    ):
        _set_if_not_none(station, target_key, _decimal_value(kpi.get(source_key)))

    environmental = data_result.get("hjgx")
    if isinstance(environmental, dict):
        for source_key, target_key in (
            ("co2", "co2_avoided"),
            ("tree", "trees_equivalent"),
            ("coal", "coal_saved"),
        ):
            _set_if_not_none(
                station, target_key, _decimal_value(environmental.get(source_key))
            )

    return station


@dataclass(slots=True)
class SemsRuntimeData:
    """Runtime data stored on the config entry."""

    api: SemsApi
    coordinator: SemsDataUpdateCoordinator
    mqtt_listener: SemsMqttListener
    mqtt_task: asyncio.Task[None]


type SemsConfigEntry = ConfigEntry[SemsRuntimeData]


@dataclass(slots=True)
class SemsData:
    """Runtime SEMS data returned by the coordinator."""

    inverters: dict[str, dict[str, Any]]
    station: dict[str, Any] | None = None
    batteries: dict[str, dict[str, Any]] | None = None
    powerflow: dict[str, Any] | None = None
    currency: str | None = None
    station_id: str | None = None
    station_name: str | None = None
    mqtt_connection_state: str = (
        "disconnected"  # Connection state: connected, connecting, disconnected, failed
    )
    mqtt_is_connected: bool = False  # Whether MQTT is currently connected
    mqtt_connection_failures: int = 0  # Number of failed connection attempts


async def async_setup(hass: HomeAssistant, config: dict):
    """Set up the sems component."""
    return True


async def async_setup_entry(hass: HomeAssistant, entry: SemsConfigEntry) -> bool:
    """Set up sems from a config entry."""
    region = entry.data.get(CONF_REGION, DEFAULT_SEMS_REGION)
    sems_api = SemsApi(
        hass, entry.data[CONF_USERNAME], entry.data[CONF_PASSWORD], region
    )
    coordinator = SemsDataUpdateCoordinator(hass, sems_api, entry)
    await coordinator.async_config_entry_first_refresh()

    mqtt_listener = SemsMqttListener(
        hass,
        sems_api,
        entry.data[CONF_STATION_ID],
        region,
        coordinator.async_apply_mqtt_powerflow_update,
        coordinator.async_apply_mqtt_status_update,
    )
    mqtt_task = hass.async_create_background_task(
        mqtt_listener.async_run(), f"{DOMAIN} live data listener"
    )
    entry.runtime_data = SemsRuntimeData(
        api=sems_api,
        coordinator=coordinator,
        mqtt_listener=mqtt_listener,
        mqtt_task=mqtt_task,
    )
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: SemsConfigEntry) -> bool:
    """Unload a config entry."""
    if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        return False

    entry.runtime_data.mqtt_listener.stop()
    entry.runtime_data.mqtt_task.cancel()
    try:
        await asyncio.gather(entry.runtime_data.mqtt_task, return_exceptions=True)
    except asyncio.CancelledError:
        pass
    return True


class SemsDataUpdateCoordinator(DataUpdateCoordinator[SemsData]):
    """Class to manage fetching data from the API."""

    def __init__(
        self, hass: HomeAssistant, sems_api: SemsApi, entry: ConfigEntry
    ) -> None:
        """Initialize."""
        self.sems_api = sems_api
        self.station_id = entry.data[CONF_STATION_ID]

        update_interval = timedelta(seconds=DEFAULT_SCAN_INTERVAL)
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=update_interval,
        )

    async def _async_update_data(self) -> SemsData:
        """Fetch data from API endpoint.

        This is the place to pre-process the data to lookup tables
        so entities can quickly look up their data.
        """
        # Note: asyncio.TimeoutError and aiohttp.ClientError are already
        # handled by the data update coordinator.
        # async with async_timeout.timeout(10):
        try:
            data_result = await self.hass.async_add_executor_job(
                self.sems_api.getData, self.station_id
            )

        except SemsRateLimitedError as err:
            raise UpdateFailed(
                f"SEMS API rate limited (retry after {err.retry_after}s)"
            ) from err
        except Exception as err:
            raise UpdateFailed(f"Error communicating with API: {err}") from err
        else:
            _LOGGER.debug("semsApi.getData result: %s", redact_for_log(data_result))

            inverters = data_result.get("inverter")
            inverters_by_sn: dict[str, dict[str, Any]] = {}
            batteries_by_sn: dict[str, dict[str, Any]] = {}
            if not inverters or not isinstance(inverters, list):
                raise UpdateFailed(
                    "Error communicating with API: invalid or missing inverter data. See debug logs."
                )

            # Get Inverter Data
            for inverter in inverters:
                inverter_full = inverter.get("invert_full")
                if not isinstance(inverter_full, dict):
                    continue

                name = inverter_full.get("name")
                sn = inverter_full.get("sn")
                if not isinstance(sn, str):
                    continue

                _LOGGER.debug(
                    "Found inverter attribute %s %s",
                    name,
                    redact_for_log(sn),
                )
                inverters_by_sn[sn] = inverter_full

                raw_batteries = inverter_full.get("more_batterys")
                if isinstance(raw_batteries, list):
                    for index, raw_battery in enumerate(raw_batteries, start=1):
                        if not isinstance(raw_battery, dict):
                            continue
                        battery_sn = raw_battery.get("batterysnmain")
                        if not isinstance(battery_sn, str) or not battery_sn:
                            continue
                        batteries_by_sn[battery_sn] = {
                            **raw_battery,
                            "name": f"Battery {index}",
                            "inverter_serial": sn,
                        }

            station_info = data_result.get("info")
            station_name = None
            if isinstance(station_info, dict):
                station_name = station_info.get("stationname")
                if not isinstance(station_name, str) or not station_name:
                    station_name = None

            for inverter_data in inverters_by_sn.values():
                inverter_data["station_id"] = self.station_id
                inverter_data["station_name"] = station_name

            # Add currency
            kpi = data_result.get("kpi")
            if not isinstance(kpi, dict):
                kpi = {}
            currency = kpi.get("currency")
            station = _normalize_station_data(data_result, kpi)

            powerflow = _normalize_rest_powerflow(data_result, kpi)
            if powerflow is not None:
                _LOGGER.debug("Found powerflow data")

            # Get MQTT connection status from the runtime data
            mqtt_connection_state = "disconnected"
            mqtt_is_connected = False
            mqtt_connection_failures = 0
            mqtt_last_message_received_at = None
            if (
                self.config_entry is not None
                and hasattr(self.config_entry, "runtime_data")
                and self.config_entry.runtime_data
            ):
                mqtt_listener = self.config_entry.runtime_data.mqtt_listener
                mqtt_connection_state = mqtt_listener.connection_state
                mqtt_is_connected = mqtt_listener.is_connected
                mqtt_connection_failures = mqtt_listener.connection_failures
                mqtt_last_message_received_at = mqtt_listener.last_message_received_at

            station["mqtt_connection_state"] = mqtt_connection_state
            station["mqtt_is_connected"] = mqtt_is_connected
            station["mqtt_connection_failures"] = mqtt_connection_failures
            station["mqtt_last_message_received_at"] = mqtt_last_message_received_at

            data = SemsData(
                inverters=inverters_by_sn,
                station=station,
                batteries=batteries_by_sn or None,
                powerflow=powerflow,
                currency=currency,
                station_id=self.station_id,
                station_name=station_name,
                mqtt_connection_state=mqtt_connection_state,
                mqtt_is_connected=mqtt_is_connected,
                mqtt_connection_failures=mqtt_connection_failures,
            )
            _LOGGER.debug(
                "Resulting data: %s",
                redact_for_log(data),
            )
            return data

    @callback
    def async_apply_mqtt_powerflow_update(self, update: dict[str, Any]) -> None:
        """Merge a live MQTT powerflow update into coordinator data."""

        if self.data is None:
            return

        station_id = update.get("station_id")
        if isinstance(station_id, str) and station_id != self.station_id:
            _LOGGER.debug("Ignoring MQTT update for a different station")
            return

        powerflow = dict(self.data.powerflow or {})
        powerflow.update(update)
        self.async_set_updated_data(replace(self.data, powerflow=powerflow))

    @callback
    def async_apply_mqtt_status_update(
        self,
        connection_state: str,
        is_connected: bool,
        connection_failures: int,
        last_message_received_at: datetime | None,
    ) -> None:
        """Publish MQTT diagnostics on the station device."""
        if self.data is None:
            return

        station = dict(self.data.station or {})
        station.update(
            {
                "mqtt_connection_state": connection_state,
                "mqtt_is_connected": is_connected,
                "mqtt_connection_failures": connection_failures,
                "mqtt_last_message_received_at": last_message_received_at,
            }
        )
        self.async_set_updated_data(replace(self.data, station=station))


# Type alias to make type inference working for pylance
type SemsCoordinator = SemsDataUpdateCoordinator
