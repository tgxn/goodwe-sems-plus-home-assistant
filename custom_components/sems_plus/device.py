"""Device helpers for the SEMS+ integration."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo

from .const import DOMAIN
from .coordinator import DeviceData, SemsPlusData
from .mapping import DEVICE_TYPE_LABELS, device_sw_version


def device_info_for_station(data: SemsPlusData, web_origin: str) -> DeviceInfo:
    """Build device info for the station (whole-system values)."""
    return DeviceInfo(
        identifiers={(DOMAIN, data.station_id)},
        name=data.station_name,
        manufacturer="GoodWe",
        model="SEMS+ Station",
        configuration_url=web_origin,
    )


def device_info_for_device(
    station_device_id: str | None, device: DeviceData
) -> DeviceInfo:
    """Build device info for one SEMS device belonging to the station."""
    model = device.meta.get("model")
    brand = device.meta.get("brand")
    info = DeviceInfo(
        identifiers={(DOMAIN, device.sn)},
        name=device.name,
        manufacturer=(brand.title() if isinstance(brand, str) and brand else "GoodWe"),
        model=(
            model
            if isinstance(model, str) and model
            else DEVICE_TYPE_LABELS.get(device.device_type, device.device_type)
        ),
        serial_number=device.sn,
        sw_version=device_sw_version(device),
    )
    if station_device_id is not None:
        info["via_device_id"] = station_device_id
    return info
