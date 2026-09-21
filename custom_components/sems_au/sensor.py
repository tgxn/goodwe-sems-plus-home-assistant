"""Support for power production statistics from GoodWe SEMS API.

For more details about this platform, please refer to the documentation at
https://github.com/TimSoethout/goodwe-sems-home-assistant
"""

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import (
    PERCENTAGE,
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfEnergy,
    UnitOfFrequency,
    UnitOfPower,
    UnitOfTemperature,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import SemsConfigEntry, SemsCoordinator, SemsData
from .const import (
    AC_CURRENT_EMPTY,
    AC_EMPTY,
    AC_FEQ_EMPTY,
    GOODWE_SPELLING,
    STATUS_LABELS,
    redact_for_log,
)
from .device import (
    device_info_for_battery,
    device_info_for_inverter,
    device_info_for_station,
)

_LOGGER = logging.getLogger(__name__)

type SemsValuePath = list[str | int]


def convert_status_to_label(status: Any) -> str:
    """Convert numeric status code to human-readable label."""
    return STATUS_LABELS.get(int(status), "Unknown")


def _percentage_handler(value: Any, _data: dict[str, Any]) -> Any:
    """Convert a 0-1 ratio to a percentage."""
    from decimal import InvalidOperation

    if value is None:
        return None
    try:
        return Decimal(str(value)) * 100
    except (TypeError, ValueError, InvalidOperation):
        return value


@dataclass(slots=True)
class SemsSensorType:
    """SEMS sensor definition."""

    device_info: DeviceInfo
    unique_id: str
    value_path: SemsValuePath
    name: str | None = None  # Name is None when it is determined by device class / UOM.
    device_class: SensorDeviceClass | None = None
    native_unit_of_measurement: str | None = None
    state_class: SensorStateClass | None = None
    empty_value: Any = None
    data_type_converter: Callable = Decimal
    custom_value_handler: Callable[[Any, dict[str, Any]], Any] | None = None


@dataclass(slots=True)
class SemsPowerflowSensorType(SemsSensorType):
    """SEMS station-level powerflow sensor definition."""


@dataclass(slots=True)
class SemsStationSensorType(SemsSensorType):
    """SEMS station-level sensor definition."""


@dataclass(slots=True)
class SemsInverterSensorType(SemsSensorType):
    """SEMS inverter sensor definition."""


@dataclass(slots=True)
class SemsBatterySensorType(SemsSensorType):
    """SEMS battery sensor definition."""


def _entity_prefix(data: SemsData) -> str:
    """Return the station-scoped unique ID prefix."""

    return data.station_id or "station"


def _inverter_entity_name(inverter_data: dict[str, Any], name: str) -> str:
    """Return a readable station-device entity name for inverter values."""

    inverter_name = inverter_data.get("name")
    if isinstance(inverter_name, str) and inverter_name.strip():
        return f"{inverter_name} {name}"
    return name


def sensor_options_for_data(data: SemsData) -> list[SemsSensorType]:
    """Build a list of sensor definitions for the given coordinator data."""

    sensors: list[SemsSensorType] = []
    currency = data.currency
    station_prefix = _entity_prefix(data)
    _LOGGER.debug("Detected currency: %s", currency)

    station_device_info = device_info_for_station(
        data.station_id, data.station_name, data.station
    )

    if data.station:
        sensors += [
            SemsStationSensorType(
                station_device_info,
                f"{station_prefix}-status",
                ["status"],
                "Station Status",
                data_type_converter=convert_status_to_label,
            ),
            SemsStationSensorType(
                station_device_info,
                f"{station_prefix}-rated-solar-capacity",
                ["rated_solar_capacity"],
                "Station Rated Solar Capacity",
                SensorDeviceClass.POWER,
                UnitOfPower.KILO_WATT,
                SensorStateClass.MEASUREMENT,
            ),
            SemsStationSensorType(
                station_device_info,
                f"{station_prefix}-rated-battery-capacity",
                ["rated_battery_capacity"],
                "Station Rated Battery Capacity",
                SensorDeviceClass.ENERGY,
                UnitOfEnergy.KILO_WATT_HOUR,
            ),
            SemsStationSensorType(
                station_device_info,
                f"{station_prefix}-current-output-power",
                ["current_output_power"],
                "Station Current Output Power",
                SensorDeviceClass.POWER,
                UnitOfPower.WATT,
                SensorStateClass.MEASUREMENT,
            ),
            SemsStationSensorType(
                station_device_info,
                f"{station_prefix}-energy-this-month",
                ["energy_this_month"],
                "Station Energy This Month",
                SensorDeviceClass.ENERGY,
                UnitOfEnergy.KILO_WATT_HOUR,
                SensorStateClass.TOTAL_INCREASING,
            ),
            SemsStationSensorType(
                station_device_info,
                f"{station_prefix}-lifetime-solar-energy",
                ["lifetime_solar_energy"],
                "Station Lifetime Solar Energy",
                SensorDeviceClass.ENERGY,
                UnitOfEnergy.KILO_WATT_HOUR,
                SensorStateClass.TOTAL_INCREASING,
            ),
            SemsStationSensorType(
                station_device_info,
                f"{station_prefix}-yield-rate",
                ["yield_rate"],
                "Station Yield Rate",
                None,
                PERCENTAGE,
                SensorStateClass.MEASUREMENT,
                custom_value_handler=_percentage_handler,
            ),
            SemsStationSensorType(
                station_device_info,
                f"{station_prefix}-income-today",
                ["income_today"],
                "Station Income Today",
                SensorDeviceClass.MONETARY,
                currency,
                SensorStateClass.TOTAL,
            ),
            SemsStationSensorType(
                station_device_info,
                f"{station_prefix}-income-total",
                ["income_total"],
                "Station Income Total",
                SensorDeviceClass.MONETARY,
                currency,
                SensorStateClass.TOTAL,
            ),
            SemsStationSensorType(
                station_device_info,
                f"{station_prefix}-co2-avoided",
                ["co2_avoided"],
                "CO2 Avoided",
                None,
                "t",
                SensorStateClass.TOTAL,
            ),
            SemsStationSensorType(
                station_device_info,
                f"{station_prefix}-coal-saved",
                ["coal_saved"],
                "Coal Saved",
                None,
                "t",
                SensorStateClass.TOTAL,
            ),
            SemsStationSensorType(
                station_device_info,
                f"{station_prefix}-trees-equivalent",
                ["trees_equivalent"],
                "Trees Equivalent",
                None,
                "trees",
                SensorStateClass.TOTAL,
            ),
        ]

    for serial_number, inverter_data in data.inverters.items():
        inverter_sensor_start = len(sensors)
        inverter_device_info = device_info_for_inverter(
            station_prefix, serial_number, inverter_data
        )
        path_to_inverter: SemsValuePath = [serial_number]

        sensors += [
            SemsInverterSensorType(
                station_device_info,
                f"{station_prefix}-{serial_number}-status",
                [*path_to_inverter, "status"],
                _inverter_entity_name(inverter_data, "Status"),
                data_type_converter=convert_status_to_label,
            ),
            SemsInverterSensorType(
                station_device_info,
                f"{station_prefix}-{serial_number}-capacity",
                [*path_to_inverter, "capacity"],
                _inverter_entity_name(inverter_data, "Rated AC Capacity"),
                SensorDeviceClass.POWER,
                UnitOfPower.KILO_WATT,
                SensorStateClass.MEASUREMENT,
            ),
            SemsInverterSensorType(
                station_device_info,
                f"{station_prefix}-{serial_number}-power",
                [*path_to_inverter, "pac"],
                _inverter_entity_name(inverter_data, "Current AC Output Power"),
                device_class=SensorDeviceClass.POWER,
                native_unit_of_measurement=UnitOfPower.WATT,
                state_class=SensorStateClass.MEASUREMENT,
            ),
            SemsInverterSensorType(
                station_device_info,
                f"{station_prefix}-{serial_number}-energy",
                [*path_to_inverter, "etotal"],
                _inverter_entity_name(inverter_data, "Lifetime Energy"),
                device_class=SensorDeviceClass.ENERGY,
                native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
                state_class=SensorStateClass.TOTAL_INCREASING,
            ),
            SemsInverterSensorType(
                station_device_info,
                f"{station_prefix}-{serial_number}-hour-total",
                [*path_to_inverter, "hour_total"],
                _inverter_entity_name(inverter_data, "Total Hours"),
                native_unit_of_measurement=UnitOfTime.HOURS,
                state_class=SensorStateClass.TOTAL_INCREASING,
            ),
            SemsInverterSensorType(
                station_device_info,
                f"{station_prefix}-{serial_number}-temperature",
                [*path_to_inverter, GOODWE_SPELLING.temperature],
                _inverter_entity_name(inverter_data, "Temperature"),
                device_class=SensorDeviceClass.TEMPERATURE,
                native_unit_of_measurement=UnitOfTemperature.CELSIUS,
                state_class=SensorStateClass.MEASUREMENT,
                empty_value=0,
            ),
            SemsInverterSensorType(
                station_device_info,
                f"{station_prefix}-{serial_number}-eday",
                [*path_to_inverter, "eday"],
                _inverter_entity_name(inverter_data, "Energy Today"),
                SensorDeviceClass.ENERGY,
                UnitOfEnergy.KILO_WATT_HOUR,
                SensorStateClass.TOTAL_INCREASING,
            ),
            SemsInverterSensorType(
                station_device_info,
                f"{station_prefix}-{serial_number}-{GOODWE_SPELLING.thisMonthTotalE}",
                [*path_to_inverter, GOODWE_SPELLING.thisMonthTotalE],
                _inverter_entity_name(inverter_data, "Energy This Month"),
                SensorDeviceClass.ENERGY,
                UnitOfEnergy.KILO_WATT_HOUR,
                SensorStateClass.TOTAL_INCREASING,
            ),
            SemsInverterSensorType(
                station_device_info,
                f"{station_prefix}-{serial_number}-{GOODWE_SPELLING.lastMonthTotalE}",
                [*path_to_inverter, GOODWE_SPELLING.lastMonthTotalE],
                _inverter_entity_name(inverter_data, "Energy Last Month"),
                SensorDeviceClass.ENERGY,
                UnitOfEnergy.KILO_WATT_HOUR,
                SensorStateClass.TOTAL_INCREASING,
            ),
            SemsInverterSensorType(
                station_device_info,
                f"{station_prefix}-{serial_number}-iday",
                [*path_to_inverter, "iday"],
                _inverter_entity_name(inverter_data, "Income Today"),
                SensorDeviceClass.MONETARY,
                currency,
                SensorStateClass.TOTAL,
            ),
            SemsInverterSensorType(
                station_device_info,
                f"{station_prefix}-{serial_number}-itotal",
                [*path_to_inverter, "itotal"],
                _inverter_entity_name(inverter_data, "Income Total"),
                SensorDeviceClass.MONETARY,
                currency,
                SensorStateClass.TOTAL,
            ),
        ]
        # Multiple strings
        sensors += [
            SemsInverterSensorType(
                station_device_info,
                f"{station_prefix}-{serial_number}-vpv{idx}",
                [*path_to_inverter, f"vpv{idx}"],
                _inverter_entity_name(inverter_data, f"PV String {idx} Voltage"),
                SensorDeviceClass.VOLTAGE,
                UnitOfElectricPotential.VOLT,
                SensorStateClass.MEASUREMENT,
                0,
            )
            for idx in range(1, 5)
            if get_value_from_path(data.inverters, [*path_to_inverter, f"vpv{idx}"])
            is not None
        ]
        sensors += [
            SemsInverterSensorType(
                station_device_info,
                f"{station_prefix}-{serial_number}-ipv{idx}",
                [*path_to_inverter, f"ipv{idx}"],
                _inverter_entity_name(inverter_data, f"PV String {idx} Current"),
                SensorDeviceClass.CURRENT,
                UnitOfElectricCurrent.AMPERE,
                SensorStateClass.MEASUREMENT,
                0,
            )
            for idx in range(1, 5)
            if get_value_from_path(data.inverters, [*path_to_inverter, f"ipv{idx}"])
            is not None
        ]
        sensors += [
            SemsInverterSensorType(
                station_device_info,
                f"{station_prefix}-{serial_number}-vac{idx}",
                [*path_to_inverter, f"vac{idx}"],
                _inverter_entity_name(inverter_data, f"Grid {idx} AC Voltage"),
                SensorDeviceClass.VOLTAGE,
                UnitOfElectricPotential.VOLT,
                SensorStateClass.MEASUREMENT,
                AC_EMPTY,
            )
            for idx in range(1, 4)
        ]
        sensors += [
            SemsInverterSensorType(
                station_device_info,
                f"{station_prefix}-{serial_number}-iac{idx}",
                [*path_to_inverter, f"iac{idx}"],
                _inverter_entity_name(inverter_data, f"Grid {idx} AC Current"),
                SensorDeviceClass.CURRENT,
                UnitOfElectricCurrent.AMPERE,
                SensorStateClass.MEASUREMENT,
                AC_CURRENT_EMPTY,
            )
            for idx in range(1, 4)
        ]
        sensors += [
            SemsInverterSensorType(
                station_device_info,
                f"{station_prefix}-{serial_number}-fac{idx}",
                [*path_to_inverter, f"fac{idx}"],
                _inverter_entity_name(inverter_data, f"Grid {idx} AC Frequency"),
                SensorDeviceClass.FREQUENCY,
                UnitOfFrequency.HERTZ,
                SensorStateClass.MEASUREMENT,
                AC_FEQ_EMPTY,
            )
            for idx in range(1, 4)
        ]
        sensors += [
            SemsInverterSensorType(
                station_device_info,
                f"{station_prefix}-{serial_number}-vbattery1",
                [*path_to_inverter, "vbattery1"],
                _inverter_entity_name(inverter_data, "Battery Voltage"),
                SensorDeviceClass.VOLTAGE,
                UnitOfElectricPotential.VOLT,
                SensorStateClass.MEASUREMENT,
            ),
            SemsInverterSensorType(
                station_device_info,
                f"{station_prefix}-{serial_number}-ibattery1",
                [*path_to_inverter, "ibattery1"],
                _inverter_entity_name(inverter_data, "Battery Current"),
                SensorDeviceClass.CURRENT,
                UnitOfElectricCurrent.AMPERE,
                SensorStateClass.MEASUREMENT,
            ),
        ]
        # Per-inverter meter and energy data (hybrid/storage inverters)
        if (
            get_value_from_path(data.inverters, [*path_to_inverter, "pmeter"])
            is not None
        ):
            sensors.append(
                SemsInverterSensorType(
                    station_device_info,
                    f"{station_prefix}-{serial_number}-pmeter",
                    [*path_to_inverter, "pmeter"],
                    _inverter_entity_name(inverter_data, "Current Grid Meter Power"),
                    SensorDeviceClass.POWER,
                    UnitOfPower.WATT,
                    SensorStateClass.MEASUREMENT,
                ),
            )
        for source_key, unique_suffix, name, device_class, unit, state_class in (
            (
                "vload",
                "vload",
                "Backup Output Voltage",
                SensorDeviceClass.VOLTAGE,
                UnitOfElectricPotential.VOLT,
                SensorStateClass.MEASUREMENT,
            ),
            (
                "iload",
                "iload",
                "Backup Output Current",
                SensorDeviceClass.CURRENT,
                UnitOfElectricCurrent.AMPERE,
                SensorStateClass.MEASUREMENT,
            ),
            (
                "pbackup",
                "pbackup",
                "Current Backup Output Power",
                SensorDeviceClass.POWER,
                UnitOfPower.WATT,
                SensorStateClass.MEASUREMENT,
            ),
            (
                "soc",
                "soc",
                "Battery State of Charge",
                SensorDeviceClass.BATTERY,
                PERCENTAGE,
                SensorStateClass.MEASUREMENT,
            ),
            (
                "soh",
                "soh",
                "Battery State of Health",
                SensorDeviceClass.BATTERY,
                PERCENTAGE,
                SensorStateClass.MEASUREMENT,
            ),
            (
                "pf",
                "pf",
                "Power Factor",
                None,
                None,
                SensorStateClass.MEASUREMENT,
            ),
            (
                "reactive_power",
                "reactive-power",
                "Reactive Power",
                None,
                "var",
                SensorStateClass.MEASUREMENT,
            ),
            (
                "leakage_current",
                "leakage-current",
                "Leakage Current",
                SensorDeviceClass.CURRENT,
                UnitOfElectricCurrent.AMPERE,
                SensorStateClass.MEASUREMENT,
            ),
            (
                "pv_power",
                "pv-power",
                "Current Inverter PV Power",
                SensorDeviceClass.POWER,
                UnitOfPower.WATT,
                SensorStateClass.MEASUREMENT,
            ),
            (
                "total_pbattery",
                "total-pbattery",
                "Current Total Battery Power",
                SensorDeviceClass.POWER,
                UnitOfPower.WATT,
                SensorStateClass.MEASUREMENT,
            ),
            (
                "eTotalBuy",
                "eTotalBuy",
                "Total Grid Import Energy",
                SensorDeviceClass.ENERGY,
                UnitOfEnergy.KILO_WATT_HOUR,
                SensorStateClass.TOTAL_INCREASING,
            ),
            (
                "eDayBuy",
                "eDayBuy",
                "Grid Import Today",
                SensorDeviceClass.ENERGY,
                UnitOfEnergy.KILO_WATT_HOUR,
                SensorStateClass.TOTAL_INCREASING,
            ),
            (
                "total_sell",
                "total-sell",
                "Total Grid Export Energy",
                SensorDeviceClass.ENERGY,
                UnitOfEnergy.KILO_WATT_HOUR,
                SensorStateClass.TOTAL_INCREASING,
            ),
            (
                "total_buy",
                "total-buy",
                "Total Grid Import Meter Energy",
                SensorDeviceClass.ENERGY,
                UnitOfEnergy.KILO_WATT_HOUR,
                SensorStateClass.TOTAL_INCREASING,
            ),
            (
                "eBatteryCharge",
                "eBatteryCharge",
                "Total Battery Charge",
                SensorDeviceClass.ENERGY,
                UnitOfEnergy.KILO_WATT_HOUR,
                SensorStateClass.TOTAL_INCREASING,
            ),
            (
                "eBatteryDischarge",
                "eBatteryDischarge",
                "Total Battery Discharge",
                SensorDeviceClass.ENERGY,
                UnitOfEnergy.KILO_WATT_HOUR,
                SensorStateClass.TOTAL_INCREASING,
            ),
            (
                "output_eday",
                "output-eday",
                "Backup Output Energy Today",
                SensorDeviceClass.ENERGY,
                UnitOfEnergy.KILO_WATT_HOUR,
                SensorStateClass.TOTAL_INCREASING,
            ),
            (
                "output_etotal",
                "output-etotal",
                "Total Backup Output Energy",
                SensorDeviceClass.ENERGY,
                UnitOfEnergy.KILO_WATT_HOUR,
                SensorStateClass.TOTAL_INCREASING,
            ),
            (
                "genset_power",
                "genset-power",
                "Current Generator Power",
                SensorDeviceClass.POWER,
                UnitOfPower.WATT,
                SensorStateClass.MEASUREMENT,
            ),
            (
                "genset_eday",
                "genset-eday",
                "Generator Energy Today",
                SensorDeviceClass.ENERGY,
                UnitOfEnergy.KILO_WATT_HOUR,
                SensorStateClass.TOTAL_INCREASING,
            ),
            (
                "genset_etotal",
                "genset-etotal",
                "Total Generator Energy",
                SensorDeviceClass.ENERGY,
                UnitOfEnergy.KILO_WATT_HOUR,
                SensorStateClass.TOTAL_INCREASING,
            ),
        ):
            if (
                get_value_from_path(data.inverters, [*path_to_inverter, source_key])
                is None
            ):
                continue
            sensors.append(
                SemsInverterSensorType(
                    station_device_info,
                    f"{station_prefix}-{serial_number}-{unique_suffix}",
                    [*path_to_inverter, source_key],
                    _inverter_entity_name(inverter_data, name),
                    device_class,
                    unit,
                    state_class,
                )
            )
        if (
            get_value_from_path(data.inverters, [*path_to_inverter, "eChargeDay"])
            is not None
        ):
            sensors.append(
                SemsInverterSensorType(
                    station_device_info,
                    f"{station_prefix}-{serial_number}-eChargeDay",
                    [*path_to_inverter, "eChargeDay"],
                    _inverter_entity_name(inverter_data, "Battery Charge Today"),
                    SensorDeviceClass.ENERGY,
                    UnitOfEnergy.KILO_WATT_HOUR,
                    SensorStateClass.TOTAL_INCREASING,
                ),
            )
        if (
            get_value_from_path(data.inverters, [*path_to_inverter, "eDischargeDay"])
            is not None
        ):
            sensors.append(
                SemsInverterSensorType(
                    station_device_info,
                    f"{station_prefix}-{serial_number}-eDischargeDay",
                    [*path_to_inverter, "eDischargeDay"],
                    _inverter_entity_name(inverter_data, "Battery Discharge Today"),
                    SensorDeviceClass.ENERGY,
                    UnitOfEnergy.KILO_WATT_HOUR,
                    SensorStateClass.TOTAL_INCREASING,
                ),
            )
        _LOGGER.debug(
            "Sensors for inverter %s: %s",
            redact_for_log(serial_number),
            redact_for_log(sensors),
        )
        for sensor in sensors[inverter_sensor_start:]:
            if isinstance(sensor, SemsInverterSensorType):
                sensor.device_info = inverter_device_info

    for battery_serial, battery_data in (data.batteries or {}).items():
        battery_device_info = device_info_for_battery(
            station_prefix, battery_serial, battery_data
        )
        battery_path: SemsValuePath = [battery_serial]
        for key, name, device_class, unit in (
            ("pbattery", "Power", SensorDeviceClass.POWER, UnitOfPower.WATT),
            (
                "vbattery",
                "Voltage",
                SensorDeviceClass.VOLTAGE,
                UnitOfElectricPotential.VOLT,
            ),
            (
                "ibattery",
                "Current",
                SensorDeviceClass.CURRENT,
                UnitOfElectricCurrent.AMPERE,
            ),
            ("soc", "State of charge", SensorDeviceClass.BATTERY, PERCENTAGE),
            ("soh", "State of health", SensorDeviceClass.BATTERY, PERCENTAGE),
            (
                "bms_temperature",
                "Temperature",
                SensorDeviceClass.TEMPERATURE,
                UnitOfTemperature.CELSIUS,
            ),
            (
                "bms_discharge_i_max",
                "Maximum discharge current",
                SensorDeviceClass.CURRENT,
                UnitOfElectricCurrent.AMPERE,
            ),
            (
                "bms_charge_i_max",
                "Maximum charge current",
                SensorDeviceClass.CURRENT,
                UnitOfElectricCurrent.AMPERE,
            ),
        ):
            if get_value_from_path(data.batteries or {}, [*battery_path, key]) is None:
                continue
            sensors.append(
                SemsBatterySensorType(
                    battery_device_info,
                    f"{station_prefix}:battery:{battery_serial}:{key}",
                    [*battery_path, key],
                    name,
                    device_class,
                    unit,
                    SensorStateClass.MEASUREMENT,
                )
            )

    # Station powerflow + SEMS charts live in `SemsData.powerflow`.
    if data.powerflow is not None:
        powerflow_prefix = f"{station_prefix}-powerflow"

        def status_value_handler(
            status_path: SemsValuePath,
        ) -> Callable[[Any, dict[str, Any]], Any]:
            """Return a handler that applies a sign depending on grid status."""

            def value_status_handler(value: Any, data: dict[str, Any]) -> Any:
                """Apply the grid status sign to the given value."""
                if value is None or value == "":
                    return None
                grid_status = get_value_from_path(data, status_path)
                if grid_status is None:
                    return value
                try:
                    return Decimal(str(value)) * int(grid_status)
                except (TypeError, ValueError):
                    return value

            return value_status_handler

        sensors += [
            SemsPowerflowSensorType(
                station_device_info,
                f"{powerflow_prefix}-load",
                ["load"],
                "Current Home Load Power",
                SensorDeviceClass.POWER,
                UnitOfPower.WATT,
                SensorStateClass.MEASUREMENT,
                custom_value_handler=status_value_handler(["loadStatus"]),
            ),
            SemsPowerflowSensorType(
                station_device_info,
                f"{powerflow_prefix}-pv",
                ["pv"],
                "Current Solar Generation Power",
                SensorDeviceClass.POWER,
                UnitOfPower.WATT,
                SensorStateClass.MEASUREMENT,
            ),
            SemsPowerflowSensorType(
                station_device_info,
                f"{powerflow_prefix}-grid",
                ["grid"],
                "Current Grid Import Export Power",
                SensorDeviceClass.POWER,
                UnitOfPower.WATT,
                SensorStateClass.MEASUREMENT,
                custom_value_handler=status_value_handler(["gridStatus"]),
            ),
            SemsPowerflowSensorType(
                station_device_info,
                f"{powerflow_prefix}-load-status",
                ["loadStatus"],
                "Current Load Flow Direction",
                None,
                None,
                SensorStateClass.MEASUREMENT,
                custom_value_handler=status_value_handler(["gridStatus"]),
            ),
            SemsPowerflowSensorType(
                station_device_info,
                f"{powerflow_prefix}-battery",
                ["battery"],
                "Current Battery Charge Discharge Power",
                SensorDeviceClass.POWER,
                UnitOfPower.WATT,
                SensorStateClass.MEASUREMENT,
                custom_value_handler=status_value_handler(["batteryStatus"]),
            ),
            SemsPowerflowSensorType(
                station_device_info,
                f"{powerflow_prefix}-genset",
                ["genset"],
                "Current Generator Power",
                SensorDeviceClass.POWER,
                UnitOfPower.WATT,
                SensorStateClass.MEASUREMENT,
            ),
            SemsPowerflowSensorType(
                station_device_info,
                f"{powerflow_prefix}-soc",
                ["soc"],
                "Battery State of Charge",
                SensorDeviceClass.BATTERY,
                PERCENTAGE,
                SensorStateClass.MEASUREMENT,
            ),
            SemsPowerflowSensorType(
                station_device_info,
                f"{powerflow_prefix}-ac-power",
                ["ac_power"],
                "Current AC Power",
                SensorDeviceClass.POWER,
                UnitOfPower.WATT,
                SensorStateClass.MEASUREMENT,
            ),
            SemsPowerflowSensorType(
                station_device_info,
                f"{powerflow_prefix}-dc-power",
                ["dc_power"],
                "Current DC Power",
                SensorDeviceClass.POWER,
                UnitOfPower.WATT,
                SensorStateClass.MEASUREMENT,
            ),
            SemsPowerflowSensorType(
                station_device_info,
                f"{powerflow_prefix}-reactive-power",
                ["reactive_power"],
                "Current Reactive Power",
                None,
                "var",
                SensorStateClass.MEASUREMENT,
            ),
            SemsPowerflowSensorType(
                station_device_info,
                f"{powerflow_prefix}-grid-frequency",
                ["grid_frequency"],
                "Current Grid Frequency",
                SensorDeviceClass.FREQUENCY,
                UnitOfFrequency.HERTZ,
                SensorStateClass.MEASUREMENT,
            ),
            SemsPowerflowSensorType(
                station_device_info,
                f"{powerflow_prefix}-power-factor",
                ["power_factor"],
                "Current Power Factor",
                None,
                None,
                SensorStateClass.MEASUREMENT,
            ),
        ]
        if data.powerflow.get(GOODWE_SPELLING.hasEnergyStatisticsCharts):
            if any(key.startswith("Charts_") for key in data.powerflow):
                sensors += [
                    SemsPowerflowSensorType(
                        station_device_info,
                        f"{powerflow_prefix}-import-energy",
                        ["Charts_buy"],
                        "Grid Import Today",
                        SensorDeviceClass.ENERGY,
                        UnitOfEnergy.KILO_WATT_HOUR,
                        SensorStateClass.TOTAL_INCREASING,
                    ),
                    SemsPowerflowSensorType(
                        station_device_info,
                        f"{powerflow_prefix}-export-energy",
                        ["Charts_sell"],
                        "Grid Export Today",
                        SensorDeviceClass.ENERGY,
                        UnitOfEnergy.KILO_WATT_HOUR,
                        SensorStateClass.TOTAL_INCREASING,
                    ),
                    SemsPowerflowSensorType(
                        station_device_info,
                        f"{powerflow_prefix}-daily-load-consumption",
                        ["Charts_consumptionOfLoad"],
                        "SEMS Daily Load Consumption",
                        SensorDeviceClass.ENERGY,
                        UnitOfEnergy.KILO_WATT_HOUR,
                        SensorStateClass.TOTAL_INCREASING,
                    ),
                    SemsPowerflowSensorType(
                        station_device_info,
                        f"{powerflow_prefix}-daily-self-use",
                        ["Charts_selfUseOfPv"],
                        "Daily Solar Self Use",
                        SensorDeviceClass.ENERGY,
                        UnitOfEnergy.KILO_WATT_HOUR,
                        SensorStateClass.TOTAL_INCREASING,
                    ),
                    SemsPowerflowSensorType(
                        station_device_info,
                        f"{powerflow_prefix}-daily-battery-charge",
                        ["Charts_charge"],
                        "SEMS Daily Battery Charge",
                        SensorDeviceClass.ENERGY,
                        UnitOfEnergy.KILO_WATT_HOUR,
                        SensorStateClass.TOTAL_INCREASING,
                    ),
                    SemsPowerflowSensorType(
                        station_device_info,
                        f"{powerflow_prefix}-daily-battery-discharge",
                        ["Charts_disCharge"],
                        "SEMS Daily Battery Discharge",
                        SensorDeviceClass.ENERGY,
                        UnitOfEnergy.KILO_WATT_HOUR,
                        SensorStateClass.TOTAL_INCREASING,
                    ),
                    SemsPowerflowSensorType(
                        station_device_info,
                        f"{powerflow_prefix}-daily-self-sufficiency-rate",
                        ["Charts_contributingRate"],
                        "Daily Self Sufficiency Rate",
                        None,
                        PERCENTAGE,
                        SensorStateClass.MEASUREMENT,
                        custom_value_handler=_percentage_handler,
                    ),
                    SemsPowerflowSensorType(
                        station_device_info,
                        f"{powerflow_prefix}-daily-self-use-rate",
                        ["Charts_selfUseRate"],
                        "Daily Solar Self Use Rate",
                        None,
                        PERCENTAGE,
                        SensorStateClass.MEASUREMENT,
                        custom_value_handler=_percentage_handler,
                    ),
                ]
            if any(key.startswith("Totals_") for key in data.powerflow):
                sensors += [
                    SemsPowerflowSensorType(
                        station_device_info,
                        f"{powerflow_prefix}-import-energy-total",
                        ["Totals_buy"],
                        "Total Grid Import",
                        SensorDeviceClass.ENERGY,
                        UnitOfEnergy.KILO_WATT_HOUR,
                        SensorStateClass.TOTAL_INCREASING,
                    ),
                    SemsPowerflowSensorType(
                        station_device_info,
                        f"{powerflow_prefix}-export-energy-total",
                        ["Totals_sell"],
                        "Total Grid Export",
                        SensorDeviceClass.ENERGY,
                        UnitOfEnergy.KILO_WATT_HOUR,
                        SensorStateClass.TOTAL_INCREASING,
                    ),
                    SemsPowerflowSensorType(
                        station_device_info,
                        f"{powerflow_prefix}-total-load-consumption",
                        ["Totals_consumptionOfLoad"],
                        "SEMS Total Load Consumption",
                        SensorDeviceClass.ENERGY,
                        UnitOfEnergy.KILO_WATT_HOUR,
                        SensorStateClass.TOTAL_INCREASING,
                    ),
                    SemsPowerflowSensorType(
                        station_device_info,
                        f"{powerflow_prefix}-total-self-use",
                        ["Totals_selfUseOfPv"],
                        "Total Solar Self Use",
                        SensorDeviceClass.ENERGY,
                        UnitOfEnergy.KILO_WATT_HOUR,
                        SensorStateClass.TOTAL_INCREASING,
                    ),
                    SemsPowerflowSensorType(
                        station_device_info,
                        f"{powerflow_prefix}-total-battery-charge",
                        ["Totals_charge"],
                        "SEMS Total Battery Charge",
                        SensorDeviceClass.ENERGY,
                        UnitOfEnergy.KILO_WATT_HOUR,
                        SensorStateClass.TOTAL_INCREASING,
                    ),
                    SemsPowerflowSensorType(
                        station_device_info,
                        f"{powerflow_prefix}-total-battery-discharge",
                        ["Totals_disCharge"],
                        "SEMS Total Battery Discharge",
                        SensorDeviceClass.ENERGY,
                        UnitOfEnergy.KILO_WATT_HOUR,
                        SensorStateClass.TOTAL_INCREASING,
                    ),
                    SemsPowerflowSensorType(
                        station_device_info,
                        f"{powerflow_prefix}-total-self-sufficiency-rate",
                        ["Totals_contributingRate"],
                        "Total Self Sufficiency Rate",
                        None,
                        PERCENTAGE,
                        SensorStateClass.MEASUREMENT,
                        custom_value_handler=_percentage_handler,
                    ),
                    SemsPowerflowSensorType(
                        station_device_info,
                        f"{powerflow_prefix}-total-self-use-rate",
                        ["Totals_selfUseRate"],
                        "Total Solar Self Use Rate",
                        None,
                        PERCENTAGE,
                        SensorStateClass.MEASUREMENT,
                        custom_value_handler=_percentage_handler,
                    ),
                ]

    # Add MQTT connection status sensors (debug/diagnostic)
    mqtt_prefix = f"{station_prefix}-mqtt"
    sensors += [
        SemsStationSensorType(
            device_info=station_device_info,
            unique_id=f"{mqtt_prefix}-connection-state",
            value_path=["mqtt_connection_state"],
            name="MQTT Connection State",
        ),
        SemsStationSensorType(
            device_info=station_device_info,
            unique_id=f"{mqtt_prefix}-connection-failures",
            value_path=["mqtt_connection_failures"],
            name="MQTT Connection Failures",
            native_unit_of_measurement="attempts",
            state_class=SensorStateClass.MEASUREMENT,
        ),
        SemsStationSensorType(
            device_info=station_device_info,
            unique_id=f"{mqtt_prefix}-last-message",
            value_path=["mqtt_last_message_received_at"],
            name="Last Live Message",
            device_class=SensorDeviceClass.TIMESTAMP,
            data_type_converter=lambda value: (
                value if isinstance(value, datetime) else None
            ),
        ),
    ]

    return sensors


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: SemsConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Add sensors for passed config_entry in HA."""
    coordinator = config_entry.runtime_data.coordinator

    sensor_options: list[SemsSensorType] = sensor_options_for_data(coordinator.data)
    sensors = []
    for sensor_option in sensor_options:
        sensor_class: type[SemsSensor]
        if isinstance(sensor_option, SemsPowerflowSensorType):
            sensor_class = SemsPowerflowSensor
        elif isinstance(sensor_option, SemsStationSensorType):
            sensor_class = SemsStationSensor
        elif isinstance(sensor_option, SemsBatterySensorType):
            sensor_class = SemsBatterySensor
        else:
            sensor_class = SemsInverterSensor

        sensors.append(
            sensor_class(
                coordinator,
                sensor_option.device_info,
                sensor_option.unique_id,
                sensor_option.name,
                sensor_option.value_path,
                sensor_option.data_type_converter,
                sensor_option.device_class,
                sensor_option.native_unit_of_measurement,
                sensor_option.state_class,
                sensor_option.empty_value,
                sensor_option.custom_value_handler,
            )
        )
    async_add_entities(sensors)


def get_value_from_path(data: dict[str, Any], path: SemsValuePath) -> Any:
    """Return the value at a nested path in a dict, or `None` if missing."""

    value: Any = data
    try:
        for key in path:
            value = value[key]
    except (KeyError, TypeError):
        return None
    return value


class SemsSensor(CoordinatorEntity[SemsCoordinator], SensorEntity):
    """Representation of a GoodWe SEMS sensor backed by the shared coordinator."""

    str_clean_regex = re.compile(r"(\d+\.?\d*)")

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: SemsCoordinator,
        device_info: DeviceInfo,
        unique_id: str,
        name: str | None,
        value_path: SemsValuePath,
        data_type_converter: Callable,
        device_class: SensorDeviceClass | None = None,
        native_unit_of_measurement: str | None = None,
        state_class: SensorStateClass | None = None,
        empty_value=None,
        custom_value_handler=None,
    ) -> None:
        """Initialize a SEMS sensor."""

        super().__init__(coordinator)
        self._value_path = value_path
        self._data_type_converter = data_type_converter
        self._empty_value = empty_value

        self._attr_unique_id = unique_id
        self._attr_device_info = device_info
        self._attr_device_class = device_class
        self._attr_native_unit_of_measurement = native_unit_of_measurement
        self._attr_state_class = state_class

        # When `name` is None, Home Assistant determines the name from
        # device class / unit (using has_entity_name).
        if name is not None:
            self._attr_name = name

        self._custom_value_handler = custom_value_handler

        raw_value = self._get_native_value_from_coordinator()

        # Disable-by-default must be decided before registry entry is created.
        if raw_value is None or (
            self._empty_value is not None and raw_value == self._empty_value
        ):
            _LOGGER.debug(
                "Disabling SemsSensor `%s` by default since initial value is None or empty (`%s`)",
                unique_id,
                raw_value,
            )
            self._attr_entity_registry_enabled_default = False

        _LOGGER.debug(
            "Created SemsSensor with id `%s`, `%s`, value path `%s`",  # , data `%s`",
            unique_id,
            name,
            value_path,
        )

    def _get_native_value_from_coordinator(self) -> Any:
        """Get the raw value from coordinator data."""

        data = self._get_data_dict()
        if data is None:
            return None
        return get_value_from_path(data, self._value_path)

    def _get_data_dict(self) -> dict[str, Any] | None:
        """Return the dict to read values from."""

        return self.coordinator.data.inverters

    @property
    def native_value(self) -> Any:
        """Return the current value."""

        value = self._get_native_value_from_coordinator()

        if isinstance(value, str):
            if match := self.str_clean_regex.search(value):
                value = match.group(1)
            else:
                # If no match found (e.g., empty string), treat as unavailable
                value = None

        if value is None:
            return None
        if self._empty_value is not None and value == self._empty_value:
            return None

        if self._custom_value_handler is not None:
            data = self._get_data_dict()
            if data is None:
                return None
            return self._custom_value_handler(value, data)

        try:
            return self._data_type_converter(value)
        except (TypeError, ValueError):
            return value


class SemsInverterSensor(SemsSensor):
    """Sensor that reads from inverter data."""

    def _get_data_dict(self) -> dict[str, Any] | None:
        """Return inverter dict."""

        return self.coordinator.data.inverters


class SemsStationSensor(SemsSensor):
    """Sensor that reads from station-level data."""

    def _get_data_dict(self) -> dict[str, Any] | None:
        """Return station dict."""

        return self.coordinator.data.station


class SemsBatterySensor(SemsSensor):
    """Sensor that reads from battery data."""

    def _get_data_dict(self) -> dict[str, Any] | None:
        """Return battery dict."""

        return self.coordinator.data.batteries


class SemsPowerflowSensor(SemsSensor):
    """Sensor that reads from station powerflow data."""

    def _get_data_dict(self) -> dict[str, Any] | None:
        """Return the station powerflow dict."""

        return self.coordinator.data.powerflow
