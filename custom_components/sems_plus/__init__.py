"""The GoodWe SEMS+ integration."""

from __future__ import annotations

from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import CONF_REGION, DEFAULT_SEMS_REGION, PLATFORMS, SEMS_REGIONS
from .coordinator import SemsPlusConfigEntry, SemsPlusCoordinator, SemsPlusRuntimeData
from .device import device_info_for_station
from .sems_api_v2 import SemsPlusClient


async def async_setup_entry(hass: HomeAssistant, entry: SemsPlusConfigEntry) -> bool:
    """Set up a SEMS+ station from a config entry."""
    region = SEMS_REGIONS[entry.data.get(CONF_REGION, DEFAULT_SEMS_REGION)]
    client = SemsPlusClient(
        async_get_clientsession(hass),
        entry.data[CONF_USERNAME],
        entry.data[CONF_PASSWORD],
        region,
    )
    coordinator = SemsPlusCoordinator(hass, entry, client)
    await coordinator.async_config_entry_first_refresh()

    # Register the station first so its devices can link to it via its registry id.
    station_device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        **device_info_for_station(coordinator.data, region.web_origin),
    )
    coordinator.station_device_id = station_device.id

    entry.runtime_data = SemsPlusRuntimeData(client=client, coordinator=coordinator)
    client.start_mqtt(
        coordinator.station_id,
        coordinator.async_handle_live,
        coordinator.async_handle_mqtt_state,
    )
    entry.async_on_unload(client.stop_mqtt)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: SemsPlusConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
