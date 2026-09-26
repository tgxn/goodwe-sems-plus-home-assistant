"""Map SEMS+ API and live MQTT values onto Home Assistant entities.

Every entity description names the `source` its value comes from, and its
`value_fn` reads only that source, so each entity is tied to one device and one
API field:

- LIVE: MQTT live feed, falling back to REST stations/flow (API power in kW)
- STATION: stations page row
- STATS: stations/statistics (daily energy, kWh)
- PRODUCTION: stations/production (income)
- BASIC: stations/basic/info
- AGGREGATE: combined from the station's battery racks
- TELEMETRY / TELECOUNTING / INFO / META / RELATED: per-device endpoints
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any, Final

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntityDescription,
)
from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    PERCENTAGE,
    EntityCategory,
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfEnergy,
    UnitOfFrequency,
    UnitOfPower,
    UnitOfReactivePower,
    UnitOfTemperature,
    UnitOfTime,
)

if TYPE_CHECKING:
    from .coordinator import DeviceData, SemsPlusData
    from .sems_api_v2 import SemsPlusClient

SOURCE_LIVE: Final = "live"
SOURCE_STATION: Final = "station"
SOURCE_STATS: Final = "statistics"
SOURCE_PRODUCTION: Final = "production"
SOURCE_BASIC: Final = "basic_info"
SOURCE_AGGREGATE: Final = "aggregate"
SOURCE_TELEMETRY: Final = "telemetry"
SOURCE_TELECOUNTING: Final = "telecounting"
SOURCE_INFO: Final = "information"
SOURCE_META: Final = "device_list"
SOURCE_RELATED: Final = "related_devices"
SOURCE_CLIENT: Final = "client"

DEVICE_TYPE_INVERTER: Final = "ENERGY_STORAGE_INTEGRATED_CABINET"
DEVICE_TYPE_BATTERY: Final = "BATTERY_RACK"
DEVICE_TYPE_DONGLE: Final = "DONGLE"

DEVICE_TYPE_LABELS: Final = {
    DEVICE_TYPE_INVERTER: "Inverter",
    DEVICE_TYPE_BATTERY: "Battery",
    DEVICE_TYPE_DONGLE: "Dongle",
}

# Status codes are not documented by SEMS+; labels are inferred from observed
# data (see API.md). Unknown codes report "unknown" with the raw code attribute.
STATION_STATUS: Final = {1: "working", 0: "waiting", 2: "fault", -1: "offline"}
DEVICE_STATUS: Final = {5: "running"}
DONGLE_STATUS: Final = {1: "online"}
BATTERY_SYSTEM_STATUS: Final = {7: "discharging"}
STATUS_UNKNOWN: Final = "unknown"

MQTT_STATES: Final = ["disconnected", "connecting", "connected", "failed"]
BATTERY_STATES: Final = ["charging", "discharging", "idle"]
GRID_STATES: Final = ["importing", "exporting", "idle"]

_KILO: Final = Decimal(1000)
_HUNDRED: Final = Decimal(100)
_MPPT_PATTERN: Final = re.compile(r"^MPPT-(\d+):(Ppv|Vpv|Ipv)$")


# ----------------------------------------------------------------------
# Value helpers
# ----------------------------------------------------------------------


def to_decimal(value: Any) -> Decimal | None:
    """Return a finite Decimal for a SEMS numeric value, or None."""
    if value in (None, "") or isinstance(value, bool):
        return None
    try:
        decimal_value = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return decimal_value if decimal_value.is_finite() else None


def _tidy(value: Decimal) -> Decimal:
    """Drop trailing zeros from a scaled value (423.72000 -> 423.72, 1.5E+3 -> 1500)."""
    normalized = value.normalize()
    if normalized.as_tuple().exponent > 0:  # type: ignore[operator]
        return normalized.quantize(Decimal(1))
    return normalized


def kw_to_w(value: Any) -> Decimal | None:
    """Convert a kW value to W."""
    decimal_value = to_decimal(value)
    return None if decimal_value is None else _tidy(decimal_value * _KILO)


def mv_to_v(value: Any) -> Decimal | None:
    """Convert a mV value to V."""
    decimal_value = to_decimal(value)
    return None if decimal_value is None else _tidy(decimal_value / _KILO)


def status_label(labels: dict[int, str], code: Any) -> str | None:
    """Return the label for a status code, or "unknown" for unmapped codes."""
    decimal_value = to_decimal(code)
    if decimal_value is None:
        return None
    return labels.get(int(decimal_value), STATUS_UNKNOWN)


def status_options(labels: dict[int, str]) -> list[str]:
    """Return the enum options for a status map."""
    return [*dict.fromkeys(labels.values()), STATUS_UNKNOWN]


@dataclass(frozen=True, slots=True)
class Factor:
    """One telemetry/telecounting/information value."""

    value: Any
    unit: str | None = None
    alias: str | None = None
    numeric: bool = False


def flatten_factors(*sources: Iterable[dict[str, Any]]) -> dict[str, Factor]:
    """Flatten factor groups (or flat factor lists) into `{code: Factor}`.

    Later sources win when a code repeats, so pass the preferred source last.
    """
    factors: dict[str, Factor] = {}
    for source in sources:
        for item in source:
            if not isinstance(item, dict):
                continue
            group_factors = item.get("factors")
            entries = group_factors if isinstance(group_factors, list) else [item]
            for entry in entries:
                if not isinstance(entry, dict) or not isinstance(
                    entry.get("code"), str
                ):
                    continue
                numeric = str(entry.get("dataType", "")).upper() == "NUMERIC"
                raw = entry.get("data")
                factors[entry["code"]] = Factor(
                    value=to_decimal(raw) if numeric else raw,
                    unit=entry.get("unit"),
                    alias=entry.get("alias"),
                    numeric=numeric,
                )
    return factors


def battery_state(flows: dict[str, list[str]]) -> str:
    """Return charging/discharging/idle from the live `flows` edges."""
    if flows.get("pBat"):
        return "discharging"
    if any("pBat" in targets for targets in flows.values()):
        return "charging"
    return "idle"


def grid_state(flows: dict[str, list[str]]) -> str:
    """Return importing/exporting/idle from the live `flows` edges."""
    if flows.get("pGrid"):
        return "importing"
    if any("pGrid" in targets for targets in flows.values()):
        return "exporting"
    return "idle"


def signed_power(value: Decimal | None, state: str) -> Decimal | None:
    """Apply the HA sign convention to a power magnitude.

    Battery: + discharging / - charging. Grid: + importing / - exporting.
    Values SEMS already sent as negative are kept as-is.
    """
    if value is None or value < 0:
        return value
    if state in ("charging", "exporting"):
        return -value
    return value


def split_positive(value: Decimal | None) -> Decimal | None:
    """Return the positive part of a signed value."""
    if value is None:
        return None
    return value if value > 0 else Decimal(0)


def split_negative(value: Decimal | None) -> Decimal | None:
    """Return the magnitude of the negative part of a signed value."""
    if value is None:
        return None
    return -value if value < 0 else Decimal(0)


def summarize_statistics(
    statistics: dict[str, Any], today: date
) -> tuple[dict[str, Decimal], dict[str, Decimal]]:
    """Return `(today, period_total)` per item from a statistics response."""
    today_values: dict[str, Decimal] = {}
    totals: dict[str, Decimal] = {}
    data_list = statistics.get("dataList")
    if not isinstance(data_list, list):
        return today_values, totals

    for series in data_list:
        if not isinstance(series, dict) or not isinstance(series.get("item"), str):
            continue
        points = series.get("statisticsList")
        if not isinstance(points, list):
            continue
        total = Decimal(0)
        for point in points:
            if not isinstance(point, dict):
                continue
            value = to_decimal(point.get("val"))
            if value is None:
                continue
            total += value
            if point.get("date") == today.isoformat():
                today_values[series["item"]] = value
        totals[series["item"]] = total
    return today_values, totals


def percentage(part: Decimal | None, whole: Decimal | None) -> Decimal | None:
    """Return part/whole as a percentage, or None when whole is zero/unknown."""
    if part is None or whole is None or whole <= 0:
        return None
    return max(Decimal(0), min(_HUNDRED, part / whole * _HUNDRED))


def rack_values(data: SemsPlusData, code: str) -> list[Decimal]:
    """Return a telemetry value from every battery rack that reports it."""
    values = []
    for device in data.devices.values():
        if device.device_type != DEVICE_TYPE_BATTERY:
            continue
        factor = device.factors.get(code)
        if factor is not None and isinstance(factor.value, Decimal):
            values.append(factor.value)
    return values


def _mean(values: list[Decimal]) -> Decimal | None:
    return sum(values, Decimal(0)) / len(values) if values else None


# ----------------------------------------------------------------------
# Entity descriptions
# ----------------------------------------------------------------------


@dataclass(frozen=True, kw_only=True)
class SemsStationSensorDescription(SensorEntityDescription):
    """Station (whole-system) sensor.

    Values come from coordinator data (`value_fn`), or for live-feed health
    from the client that owns the MQTT connection (`client_fn`).
    """

    source: str
    value_fn: Callable[[SemsPlusData], Any] | None = None
    client_fn: Callable[[SemsPlusClient], Any] | None = None
    unit_fn: Callable[[SemsPlusData], str | None] | None = None
    code_fn: Callable[[SemsPlusData], Any] | None = None


@dataclass(frozen=True, kw_only=True)
class SemsDeviceSensorDescription(SensorEntityDescription):
    """Sensor for one SEMS device (inverter, battery rack, dongle)."""

    source: str
    value_fn: Callable[[SemsPlusData, DeviceData], Any]
    code_fn: Callable[[SemsPlusData, DeviceData], Any] | None = None
    placeholders: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, kw_only=True)
class SemsStationBinarySensorDescription(BinarySensorEntityDescription):
    """Station binary sensor (coordinator data or client, as for sensors)."""

    source: str
    value_fn: Callable[[SemsPlusData], bool | None] | None = None
    client_fn: Callable[[SemsPlusClient], bool | None] | None = None


@dataclass(frozen=True, kw_only=True)
class SemsDeviceBinarySensorDescription(BinarySensorEntityDescription):
    """Binary sensor for one SEMS device."""

    source: str
    value_fn: Callable[[SemsPlusData, DeviceData], bool | None]


def _live(code: str) -> Callable[[SemsPlusData], Decimal | None]:
    return lambda data: data.live.get(code)


def _live_power(code: str) -> Callable[[SemsPlusData], Decimal | None]:
    return lambda data: kw_to_w(data.live.get(code))


def _grid_power(data: SemsPlusData) -> Decimal | None:
    return signed_power(kw_to_w(data.live.get("pGrid")), grid_state(data.flows))


def _battery_power(data: SemsPlusData) -> Decimal | None:
    return signed_power(kw_to_w(data.live.get("pBat")), battery_state(data.flows))


def _station(key: str) -> Callable[[SemsPlusData], Decimal | None]:
    return lambda data: to_decimal(data.station.get(key))


def _basic(key: str) -> Callable[[SemsPlusData], Decimal | None]:
    return lambda data: to_decimal(data.basic.get(key))


def _stats_today(item: str) -> Callable[[SemsPlusData], Decimal | None]:
    return lambda data: data.stats_today.get(item)


def _stats_month(item: str) -> Callable[[SemsPlusData], Decimal | None]:
    return lambda data: data.stats_month.get(item)


def _self_sufficiency(data: SemsPlusData) -> Decimal | None:
    load = data.stats_today.get("proConsumStats")
    imported = data.stats_today.get("proPurchaseStats")
    if load is None or imported is None:
        return None
    return percentage(load - imported, load)


def _self_use_rate(data: SemsPlusData) -> Decimal | None:
    return percentage(
        data.stats_today.get("proSelfConsumStats"),
        data.stats_today.get("proSystemTotalStats"),
    )


def _battery_system_code(data: SemsPlusData) -> Any:
    for device in data.devices.values():
        for related in device.related:
            if related.get("type") == "BAT_SYS":
                return related.get("status")
    return None


def _energy(
    key: str, source: str, value_fn: Callable[[SemsPlusData], Any]
) -> SemsStationSensorDescription:
    return SemsStationSensorDescription(
        key=key,
        translation_key=key,
        source=source,
        value_fn=value_fn,
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        state_class=SensorStateClass.TOTAL_INCREASING,
    )


def _power(
    key: str, source: str, value_fn: Callable[[SemsPlusData], Any]
) -> SemsStationSensorDescription:
    return SemsStationSensorDescription(
        key=key,
        translation_key=key,
        source=source,
        value_fn=value_fn,
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.WATT,
        state_class=SensorStateClass.MEASUREMENT,
    )


STATION_SENSORS: tuple[SemsStationSensorDescription, ...] = (
    # Real-time power (MQTT, REST stations/flow fallback)
    _power("solar_power", SOURCE_LIVE, _live_power("pSystem")),
    _power("load_power", SOURCE_LIVE, _live_power("pConsum")),
    _power("grid_power", SOURCE_LIVE, _grid_power),
    _power(
        "grid_import_power", SOURCE_LIVE, lambda data: split_positive(_grid_power(data))
    ),
    _power(
        "grid_export_power", SOURCE_LIVE, lambda data: split_negative(_grid_power(data))
    ),
    _power("battery_power", SOURCE_LIVE, _battery_power),
    _power(
        "battery_charge_power",
        SOURCE_LIVE,
        lambda data: split_negative(_battery_power(data)),
    ),
    _power(
        "battery_discharge_power",
        SOURCE_LIVE,
        lambda data: split_positive(_battery_power(data)),
    ),
    _power("inverter_power", SOURCE_LIVE, _live_power("pAc")),
    _power("pv_input_power", SOURCE_LIVE, _live_power("pDc")),
    SemsStationSensorDescription(
        key="reactive_power",
        translation_key="reactive_power",
        source=SOURCE_LIVE,
        value_fn=_live_power("qAc"),
        device_class=SensorDeviceClass.REACTIVE_POWER,
        native_unit_of_measurement=UnitOfReactivePower.VOLT_AMPERE_REACTIVE,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    SemsStationSensorDescription(
        key="grid_frequency",
        translation_key="grid_frequency",
        source=SOURCE_LIVE,
        value_fn=_live("fAc"),
        device_class=SensorDeviceClass.FREQUENCY,
        native_unit_of_measurement=UnitOfFrequency.HERTZ,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    SemsStationSensorDescription(
        key="power_factor",
        translation_key="power_factor",
        source=SOURCE_LIVE,
        value_fn=_live("pf"),
        device_class=SensorDeviceClass.POWER_FACTOR,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    SemsStationSensorDescription(
        key="battery_soc",
        translation_key="battery_soc",
        source=SOURCE_LIVE,
        value_fn=_live("soc"),
        device_class=SensorDeviceClass.BATTERY,
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    SemsStationSensorDescription(
        key="battery_state",
        translation_key="battery_state",
        source=SOURCE_LIVE,
        value_fn=lambda data: battery_state(data.flows) if data.live else None,
        device_class=SensorDeviceClass.ENUM,
        options=BATTERY_STATES,
    ),
    SemsStationSensorDescription(
        key="grid_state",
        translation_key="grid_state",
        source=SOURCE_LIVE,
        value_fn=lambda data: grid_state(data.flows) if data.live else None,
        device_class=SensorDeviceClass.ENUM,
        options=GRID_STATES,
    ),
    SemsStationSensorDescription(
        key="last_live_update",
        translation_key="last_live_update",
        source=SOURCE_LIVE,
        value_fn=lambda data: data.live_at,
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    # Daily and month-to-date energy (stations/statistics)
    _energy("solar_energy_today", SOURCE_STATS, _stats_today("proSystemTotalStats")),
    _energy("solar_energy_month", SOURCE_STATS, _stats_month("proSystemTotalStats")),
    _energy("grid_import_today", SOURCE_STATS, _stats_today("proPurchaseStats")),
    _energy("grid_import_month", SOURCE_STATS, _stats_month("proPurchaseStats")),
    _energy("grid_export_today", SOURCE_STATS, _stats_today("proGridStats")),
    _energy("grid_export_month", SOURCE_STATS, _stats_month("proGridStats")),
    _energy("load_energy_today", SOURCE_STATS, _stats_today("proConsumStats")),
    _energy("load_energy_month", SOURCE_STATS, _stats_month("proConsumStats")),
    _energy("self_use_energy_today", SOURCE_STATS, _stats_today("proSelfConsumStats")),
    _energy("self_use_energy_month", SOURCE_STATS, _stats_month("proSelfConsumStats")),
    _energy("battery_charge_today", SOURCE_STATS, _stats_today("proCharStats")),
    _energy("battery_charge_month", SOURCE_STATS, _stats_month("proCharStats")),
    _energy("battery_discharge_today", SOURCE_STATS, _stats_today("proDischarStats")),
    _energy("battery_discharge_month", SOURCE_STATS, _stats_month("proDischarStats")),
    SemsStationSensorDescription(
        key="self_sufficiency_today",
        translation_key="self_sufficiency_today",
        source=SOURCE_STATS,
        value_fn=_self_sufficiency,
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
    ),
    SemsStationSensorDescription(
        key="self_use_rate_today",
        translation_key="self_use_rate_today",
        source=SOURCE_STATS,
        value_fn=_self_use_rate,
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
    ),
    SemsStationSensorDescription(
        key="income_today",
        translation_key="income_today",
        source=SOURCE_PRODUCTION,
        value_fn=lambda data: to_decimal(data.production.get("profitProStats")),
        unit_fn=lambda data: data.production.get("currency") or None,
        device_class=SensorDeviceClass.MONETARY,
        state_class=SensorStateClass.TOTAL,
        suggested_display_precision=2,
    ),
    # Station information
    SemsStationSensorDescription(
        key="status",
        translation_key="station_status",
        source=SOURCE_STATION,
        value_fn=lambda data: status_label(STATION_STATUS, data.station.get("status")),
        code_fn=lambda data: data.station.get("status"),
        device_class=SensorDeviceClass.ENUM,
        options=status_options(STATION_STATUS),
    ),
    SemsStationSensorDescription(
        key="full_load_hours_today",
        translation_key="full_load_hours_today",
        source=SOURCE_STATION,
        value_fn=_station("fullHourToday"),
        native_unit_of_measurement=UnitOfTime.HOURS,
        device_class=SensorDeviceClass.DURATION,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    SemsStationSensorDescription(
        key="inverter_capacity",
        translation_key="inverter_capacity",
        source=SOURCE_STATION,
        value_fn=_station("installedPower"),
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SemsStationSensorDescription(
        key="pv_capacity",
        translation_key="pv_capacity",
        source=SOURCE_BASIC,
        value_fn=_basic("pvCapacity"),
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SemsStationSensorDescription(
        key="battery_capacity",
        translation_key="battery_capacity",
        source=SOURCE_BASIC,
        value_fn=_basic("batteryCapacity"),
        device_class=SensorDeviceClass.ENERGY_STORAGE,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SemsStationSensorDescription(
        key="battery_system_status",
        translation_key="battery_system_status",
        source=SOURCE_RELATED,
        value_fn=lambda data: status_label(
            BATTERY_SYSTEM_STATUS, _battery_system_code(data)
        ),
        code_fn=_battery_system_code,
        device_class=SensorDeviceClass.ENUM,
        options=status_options(BATTERY_SYSTEM_STATUS),
    ),
    # Battery values combined across racks
    SemsStationSensorDescription(
        key="battery_soh",
        translation_key="battery_soh",
        source=SOURCE_AGGREGATE,
        value_fn=lambda data: _mean(rack_values(data, "soh")),
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
    ),
    SemsStationSensorDescription(
        key="battery_cell_temperature_max",
        translation_key="battery_cell_temperature_max",
        source=SOURCE_AGGREGATE,
        value_fn=lambda data: max(rack_values(data, "tempMaxCell"), default=None),
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    SemsStationSensorDescription(
        key="battery_cell_temperature_min",
        translation_key="battery_cell_temperature_min",
        source=SOURCE_AGGREGATE,
        value_fn=lambda data: min(rack_values(data, "tempMinCell"), default=None),
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    SemsStationSensorDescription(
        key="battery_cell_voltage_max",
        translation_key="battery_cell_voltage_max",
        source=SOURCE_AGGREGATE,
        value_fn=lambda data: mv_to_v(max(rack_values(data, "vMaxCell"), default=None)),
        device_class=SensorDeviceClass.VOLTAGE,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=3,
    ),
    SemsStationSensorDescription(
        key="battery_cell_voltage_min",
        translation_key="battery_cell_voltage_min",
        source=SOURCE_AGGREGATE,
        value_fn=lambda data: mv_to_v(min(rack_values(data, "vMinCell"), default=None)),
        device_class=SensorDeviceClass.VOLTAGE,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=3,
    ),
    # Live feed diagnostics
    SemsStationSensorDescription(
        key="live_feed_state",
        translation_key="live_feed_state",
        source=SOURCE_CLIENT,
        client_fn=lambda client: client.mqtt_state,
        device_class=SensorDeviceClass.ENUM,
        options=MQTT_STATES,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SemsStationSensorDescription(
        key="last_live_message",
        translation_key="last_live_message",
        source=SOURCE_CLIENT,
        client_fn=lambda client: client.mqtt_last_message_at,
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SemsStationSensorDescription(
        key="live_feed_failures",
        translation_key="live_feed_failures",
        source=SOURCE_CLIENT,
        client_fn=lambda client: client.mqtt_connection_failures,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SemsStationSensorDescription(
        key="live_feed_messages",
        translation_key="live_feed_messages",
        source=SOURCE_CLIENT,
        client_fn=lambda client: client.mqtt_messages_received,
        state_class=SensorStateClass.TOTAL_INCREASING,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
)

STATION_BINARY_SENSORS: tuple[SemsStationBinarySensorDescription, ...] = (
    SemsStationBinarySensorDescription(
        key="live_feed_connected",
        translation_key="live_feed_connected",
        source=SOURCE_CLIENT,
        client_fn=lambda client: client.mqtt_state == "connected",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
)


# Device sensors -------------------------------------------------------


def _factor(code: str, convert: Callable[[Any], Any] | None = None) -> Callable:
    def value_fn(data: SemsPlusData, device: DeviceData) -> Any:
        factor = device.factors.get(code)
        if factor is None:
            return None
        return convert(factor.value) if convert else factor.value

    return value_fn


def _meta(key: str) -> Callable[[SemsPlusData, DeviceData], Any]:
    return lambda data, device: device.meta.get(key)


def _rack_power(data: SemsPlusData, device: DeviceData) -> Decimal | None:
    factor = device.factors.get("pBat")
    value = kw_to_w(factor.value) if factor else None
    return signed_power(value, battery_state(data.flows))


def _device_status(labels: dict[int, str]) -> SemsDeviceSensorDescription:
    return SemsDeviceSensorDescription(
        key="status",
        translation_key="device_status",
        source=SOURCE_META,
        value_fn=lambda data, device: status_label(labels, device.meta.get("status")),
        code_fn=lambda data, device: device.meta.get("status"),
        device_class=SensorDeviceClass.ENUM,
        options=status_options(labels),
    )


def _device_power(
    key: str, source: str, value_fn: Callable, enabled: bool = True
) -> SemsDeviceSensorDescription:
    return SemsDeviceSensorDescription(
        key=key,
        translation_key=key,
        source=source,
        value_fn=value_fn,
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.WATT,
        state_class=SensorStateClass.MEASUREMENT,
        entity_registry_enabled_default=enabled,
    )


def _device_energy(key: str, code: str) -> SemsDeviceSensorDescription:
    return SemsDeviceSensorDescription(
        key=key,
        translation_key=key,
        source=SOURCE_TELECOUNTING,
        value_fn=_factor(code),
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        state_class=SensorStateClass.TOTAL_INCREASING,
    )


def _device_measurement(
    key: str,
    code: str,
    device_class: SensorDeviceClass | None,
    unit: str | None,
    *,
    convert: Callable[[Any], Any] | None = None,
    precision: int | None = None,
    enabled: bool = True,
    category: EntityCategory | None = None,
) -> SemsDeviceSensorDescription:
    return SemsDeviceSensorDescription(
        key=key,
        translation_key=key,
        source=SOURCE_TELEMETRY,
        value_fn=_factor(code, convert),
        device_class=device_class,
        native_unit_of_measurement=unit,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=precision,
        entity_registry_enabled_default=enabled,
        entity_category=category,
    )


def _charge_counters() -> tuple[SemsDeviceSensorDescription, ...]:
    return tuple(
        _device_energy(f"{kind}_{period}", f"{code}{suffix}")
        for kind, code in (
            ("battery_charge", "proCharStats"),
            ("battery_discharge", "proDischarStats"),
        )
        for period, suffix in (
            ("today", "Today"),
            ("month", "Month"),
            ("total", "Total"),
        )
    )


INVERTER_SENSORS: tuple[SemsDeviceSensorDescription, ...] = (
    _device_status(DEVICE_STATUS),
    _device_power("ac_power", SOURCE_TELEMETRY, _factor("pAc", kw_to_w)),
    _device_power("pv_input_power", SOURCE_TELEMETRY, _factor("pDc", kw_to_w)),
    _device_power("backup_power", SOURCE_TELEMETRY, _factor("pBackup", kw_to_w)),
    SemsDeviceSensorDescription(
        key="reactive_power",
        translation_key="reactive_power",
        source=SOURCE_TELEMETRY,
        value_fn=_factor("qAc", kw_to_w),
        device_class=SensorDeviceClass.REACTIVE_POWER,
        native_unit_of_measurement=UnitOfReactivePower.VOLT_AMPERE_REACTIVE,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    _device_measurement(
        "grid_voltage", "Vac", SensorDeviceClass.VOLTAGE, UnitOfElectricPotential.VOLT
    ),
    _device_measurement(
        "grid_current", "Iac", SensorDeviceClass.CURRENT, UnitOfElectricCurrent.AMPERE
    ),
    _device_measurement(
        "grid_frequency", "Fac", SensorDeviceClass.FREQUENCY, UnitOfFrequency.HERTZ
    ),
    _device_measurement(
        "backup_voltage",
        "vBackupPhA",
        SensorDeviceClass.VOLTAGE,
        UnitOfElectricPotential.VOLT,
    ),
    _device_measurement(
        "backup_current",
        "aBackupPhA",
        SensorDeviceClass.CURRENT,
        UnitOfElectricCurrent.AMPERE,
    ),
    _device_measurement(
        "temperature",
        "Temperature",
        SensorDeviceClass.TEMPERATURE,
        UnitOfTemperature.CELSIUS,
    ),
    SemsDeviceSensorDescription(
        key="working_hours",
        translation_key="working_hours",
        source=SOURCE_TELEMETRY,
        value_fn=_factor("hTotal"),
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.HOURS,
        state_class=SensorStateClass.TOTAL_INCREASING,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    _device_measurement(
        "insulation_resistance",
        "ISOLimit",
        None,
        "kΩ",
        enabled=False,
        category=EntityCategory.DIAGNOSTIC,
    ),
    SemsDeviceSensorDescription(
        key="rated_power",
        translation_key="rated_power",
        source=SOURCE_TELECOUNTING,
        value_fn=_factor("ratedPower"),
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
    _device_energy("pv_energy_today", "proPvStatsToday"),
    _device_energy("pv_energy_month", "proPvStatsMonth"),
    _device_energy("pv_energy_total", "proPvStatsTotal"),
    *_charge_counters(),
)

BATTERY_SENSORS: tuple[SemsDeviceSensorDescription, ...] = (
    _device_status(DEVICE_STATUS),
    SemsDeviceSensorDescription(
        key="soc",
        translation_key="soc",
        source=SOURCE_TELEMETRY,
        value_fn=_factor("soc"),
        device_class=SensorDeviceClass.BATTERY,
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    _device_measurement("soh", "soh", None, PERCENTAGE, precision=0),
    _device_power("power", SOURCE_TELEMETRY, _rack_power),
    _device_power(
        "charge_power",
        SOURCE_TELEMETRY,
        lambda data, device: split_negative(_rack_power(data, device)),
    ),
    _device_power(
        "discharge_power",
        SOURCE_TELEMETRY,
        lambda data, device: split_positive(_rack_power(data, device)),
    ),
    _device_measurement(
        "voltage", "voltage", SensorDeviceClass.VOLTAGE, UnitOfElectricPotential.VOLT
    ),
    _device_measurement(
        "max_charge_power",
        "pMaxChar",
        SensorDeviceClass.POWER,
        UnitOfPower.WATT,
        convert=kw_to_w,
        precision=0,
    ),
    _device_measurement(
        "max_discharge_power",
        "pMaxDischar",
        SensorDeviceClass.POWER,
        UnitOfPower.WATT,
        convert=kw_to_w,
        precision=0,
    ),
    _device_measurement(
        "cell_temperature_max",
        "tempMaxCell",
        SensorDeviceClass.TEMPERATURE,
        UnitOfTemperature.CELSIUS,
    ),
    _device_measurement(
        "cell_temperature_min",
        "tempMinCell",
        SensorDeviceClass.TEMPERATURE,
        UnitOfTemperature.CELSIUS,
    ),
    _device_measurement(
        "cell_voltage_max",
        "vMaxCell",
        SensorDeviceClass.VOLTAGE,
        UnitOfElectricPotential.VOLT,
        convert=mv_to_v,
        precision=3,
    ),
    _device_measurement(
        "cell_voltage_min",
        "vMinCell",
        SensorDeviceClass.VOLTAGE,
        UnitOfElectricPotential.VOLT,
        convert=mv_to_v,
        precision=3,
    ),
    SemsDeviceSensorDescription(
        key="cell_count",
        translation_key="cell_count",
        source=SOURCE_TELEMETRY,
        value_fn=_factor("serCellTotal", to_decimal),
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
    *(
        _device_energy(f"{kind}_{period}", f"{code}{suffix}")
        for kind, code in (("charge", "proCharStats"), ("discharge", "proDischarStats"))
        for period, suffix in (
            ("today", "Today"),
            ("month", "Month"),
            ("total", "Total"),
        )
    ),
)

DONGLE_SENSORS: tuple[SemsDeviceSensorDescription, ...] = (
    _device_status(DONGLE_STATUS),
    SemsDeviceSensorDescription(
        key="communication_mode",
        translation_key="communication_mode",
        source=SOURCE_META,
        value_fn=_meta("communicationMode"),
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SemsDeviceSensorDescription(
        key="signal_strength",
        translation_key="signal_strength",
        source=SOURCE_META,
        value_fn=lambda data, device: to_decimal(
            device.meta.get("wirelessSignalStrength")
        ),
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
)

DEVICE_SENSORS: dict[str, tuple[SemsDeviceSensorDescription, ...]] = {
    DEVICE_TYPE_INVERTER: INVERTER_SENSORS,
    DEVICE_TYPE_BATTERY: BATTERY_SENSORS,
    DEVICE_TYPE_DONGLE: DONGLE_SENSORS,
}

DEVICE_BINARY_SENSORS: dict[str, tuple[SemsDeviceBinarySensorDescription, ...]] = {
    DEVICE_TYPE_INVERTER: (
        SemsDeviceBinarySensorDescription(
            key="on_grid",
            translation_key="on_grid",
            source=SOURCE_INFO,
            value_fn=lambda data, device: (
                None
                if to_decimal(device.info.get("gridConnStu")) is None
                else to_decimal(device.info.get("gridConnStu")) == 1
            ),
        ),
    ),
}

# Factor codes handled by the tables above (or intentionally skipped); any
# other numeric factor becomes a disabled "extra" sensor so new data surfaces.
_HANDLED_CODES: Final = {
    DEVICE_TYPE_INVERTER: {
        "sn",
        "pAc",
        "pDc",
        "pBackup",
        "qAc",
        "Vac",
        "Iac",
        "Fac",
        "vBackupPhA",
        "aBackupPhA",
        "Temperature",
        "hTotal",
        "ISOLimit",
        "ratedPower",
    },
    DEVICE_TYPE_BATTERY: {
        "sn",
        "soc",
        "soh",
        "pBat",
        "voltage",
        "pMaxChar",
        "pMaxDischar",
        "tempMaxCell",
        "tempMinCell",
        "vMaxCell",
        "vMinCell",
        "serCellTotal",
        "version",
        "dcdcVersion",
    },
}
# Period counters: Today/Month/Total are mapped, Week/Year are skipped.
_COUNTER_PATTERN: Final = re.compile(
    r"^pro(Pv|Char|Dischar)Stats(Today|Week|Month|Year|Total)$"
)

_UNIT_MAP: Final[dict[str, tuple[SensorDeviceClass | None, str | None, Callable]]] = {
    "kW": (SensorDeviceClass.POWER, UnitOfPower.WATT, kw_to_w),
    "kWh": (SensorDeviceClass.ENERGY, UnitOfEnergy.KILO_WATT_HOUR, to_decimal),
    "V": (SensorDeviceClass.VOLTAGE, UnitOfElectricPotential.VOLT, to_decimal),
    "mV": (SensorDeviceClass.VOLTAGE, UnitOfElectricPotential.VOLT, mv_to_v),
    "A": (SensorDeviceClass.CURRENT, UnitOfElectricCurrent.AMPERE, to_decimal),
    "Hz": (SensorDeviceClass.FREQUENCY, UnitOfFrequency.HERTZ, to_decimal),
    "℃": (SensorDeviceClass.TEMPERATURE, UnitOfTemperature.CELSIUS, to_decimal),
    "%": (None, PERCENTAGE, to_decimal),
    "kVar": (
        SensorDeviceClass.REACTIVE_POWER,
        UnitOfReactivePower.VOLT_AMPERE_REACTIVE,
        kw_to_w,
    ),
    "H": (SensorDeviceClass.DURATION, UnitOfTime.HOURS, to_decimal),
}


def _mppt_descriptions(index: str) -> tuple[SemsDeviceSensorDescription, ...]:
    placeholders = {"string": index}
    return (
        SemsDeviceSensorDescription(
            key=f"pv{index}_power",
            translation_key="pv_string_power",
            placeholders=placeholders,
            source=SOURCE_TELEMETRY,
            value_fn=_factor(f"MPPT-{index}:Ppv", kw_to_w),
            device_class=SensorDeviceClass.POWER,
            native_unit_of_measurement=UnitOfPower.WATT,
            state_class=SensorStateClass.MEASUREMENT,
        ),
        SemsDeviceSensorDescription(
            key=f"pv{index}_voltage",
            translation_key="pv_string_voltage",
            placeholders=placeholders,
            source=SOURCE_TELEMETRY,
            value_fn=_factor(f"MPPT-{index}:Vpv"),
            device_class=SensorDeviceClass.VOLTAGE,
            native_unit_of_measurement=UnitOfElectricPotential.VOLT,
            state_class=SensorStateClass.MEASUREMENT,
        ),
        SemsDeviceSensorDescription(
            key=f"pv{index}_current",
            translation_key="pv_string_current",
            placeholders=placeholders,
            source=SOURCE_TELEMETRY,
            value_fn=_factor(f"MPPT-{index}:Ipv"),
            device_class=SensorDeviceClass.CURRENT,
            native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
            state_class=SensorStateClass.MEASUREMENT,
        ),
    )


def _extra_description(code: str, factor: Factor) -> SemsDeviceSensorDescription:
    device_class, unit, convert = _UNIT_MAP.get(
        factor.unit or "", (None, factor.unit, to_decimal)
    )
    alias = (factor.alias or code).replace("_", " ").strip().capitalize()
    return SemsDeviceSensorDescription(
        key=f"extra_{re.sub(r'[^a-z0-9]+', '_', code.lower()).strip('_')}",
        name=f"{alias} ({code})",
        source=SOURCE_TELEMETRY,
        value_fn=_factor(code, convert),
        device_class=device_class,
        native_unit_of_measurement=unit,
        state_class=(
            SensorStateClass.TOTAL_INCREASING
            if device_class == SensorDeviceClass.ENERGY
            else SensorStateClass.MEASUREMENT
        ),
        entity_registry_enabled_default=False,
    )


def device_sensor_descriptions(
    device: DeviceData,
) -> list[SemsDeviceSensorDescription]:
    """Return the sensor descriptions for one device, based on its factors."""
    descriptions = list(DEVICE_SENSORS.get(device.device_type, ()))
    handled = _HANDLED_CODES.get(device.device_type, set())

    mppt_indexes = sorted(
        {m.group(1) for code in device.factors if (m := _MPPT_PATTERN.match(code))},
        key=int,
    )
    for index in mppt_indexes:
        descriptions.extend(_mppt_descriptions(index))

    for code, factor in device.factors.items():
        if (
            code in handled
            or not factor.numeric
            or _MPPT_PATTERN.match(code)
            or _COUNTER_PATTERN.match(code)
        ):
            continue
        descriptions.append(_extra_description(code, factor))
    return descriptions


def device_sw_version(device: DeviceData) -> str | None:
    """Return the firmware version to show on the HA device."""
    if device.device_type == DEVICE_TYPE_INVERTER:
        version = device.info.get("safetyVersion")
    elif device.device_type == DEVICE_TYPE_DONGLE:
        version = device.info.get("commModuleVer")
    elif device.device_type == DEVICE_TYPE_BATTERY:
        bms = device.factors.get("version")
        dcdc = device.factors.get("dcdcVersion")
        parts = [
            f"{label} {factor.value}"
            for label, factor in (("BMS", bms), ("DCDC", dcdc))
            if factor is not None and factor.value
        ]
        version = ", ".join(parts) or None
    else:
        version = None
    return str(version) if version not in (None, "") else None


def parse_flow_live(
    flow: dict[str, Any],
) -> tuple[dict[str, Decimal], dict[str, list[str]]]:
    """Convert a REST stations/flow response into live values and flows."""
    values = {
        code: value
        for code in ("pSystem", "pAc", "pBat", "pGrid", "soc", "pConsum")
        if (value := to_decimal(flow.get(code))) is not None
    }
    flows: dict[str, list[str]] = {}
    raw_flows = flow.get("flows")
    if isinstance(raw_flows, dict):
        for source, targets in raw_flows.items():
            if isinstance(source, str) and isinstance(targets, list):
                flows[source] = [t for t in targets if isinstance(t, str)]
    return values, flows
