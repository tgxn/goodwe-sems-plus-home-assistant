"""Device helpers for the SEMS integration."""

from typing import Any

from homeassistant.helpers.device_registry import DeviceInfo

from .const import DOMAIN


def device_info_for_station(
    station_id: str | None,
    station_name: str | None,
    station_data: dict[str, Any] | None = None,
) -> DeviceInfo:
    """Build device info for a SEMS station."""

    station_data = station_data or {}
    identifier = station_id or "station"
    name = station_name or "SEMS Station"

    if not isinstance(name, str) or not name.strip():
        name = "SEMS Station"

    return DeviceInfo(
        identifiers={(DOMAIN, str(identifier))},
        name=name,
        manufacturer="GoodWe",
        model=station_data.get("powerstation_type", "SEMS Station"),
        configuration_url=(
            f"https://semsportal.com/PowerStation/PowerStatusSnMin/{identifier}"
            if identifier
            else None
        ),
    )


def device_info_for_inverter(
    station_id: str,
    serial_number: str,
    inverter_data: dict[str, Any],
) -> DeviceInfo:
    """Build device info for an inverter belonging to a station."""
    firmware = inverter_data.get("firmwareversion")
    return DeviceInfo(
        identifiers={(DOMAIN, f"{station_id}:inverter:{serial_number}")},
        name=inverter_data.get("name") or f"Inverter {serial_number}",
        manufacturer="GoodWe",
        model=inverter_data.get("model_type", "Inverter"),
        sw_version=str(firmware) if firmware not in (None, "") else None,
        via_device=(DOMAIN, station_id),
    )


def device_info_for_battery(
    station_id: str,
    serial_number: str,
    battery_data: dict[str, Any],
) -> DeviceInfo:
    """Build device info for a battery belonging to a station."""
    firmware = battery_data.get("bmssoftwareversion")
    return DeviceInfo(
        identifiers={(DOMAIN, f"{station_id}:battery:{serial_number}")},
        name=battery_data.get("name") or f"Battery {serial_number}",
        manufacturer="GoodWe",
        model="Battery",
        sw_version=str(firmware) if firmware not in (None, "") else None,
        via_device=(DOMAIN, station_id),
    )
