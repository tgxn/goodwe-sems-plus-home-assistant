"""Binary sensors for the GoodWe SEMS integration."""

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import SemsConfigEntry, SemsCoordinator
from .device import device_info_for_station


class SemsMqttConnectedBinarySensor(
    CoordinatorEntity[SemsCoordinator], BinarySensorEntity
):
    """Report whether the station live-data connection is active."""

    _attr_has_entity_name = True
    _attr_name = "MQTT connected"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    def __init__(self, coordinator: SemsCoordinator) -> None:
        """Initialize the station MQTT connectivity sensor."""
        super().__init__(coordinator)
        data = coordinator.data
        self._attr_unique_id = f"{data.station_id}:station:mqtt_connected"
        self._attr_device_info = device_info_for_station(
            data.station_id, data.station_name, data.station
        )

    @property
    def is_on(self) -> bool:
        """Return whether MQTT is connected."""
        return bool((self.coordinator.data.station or {}).get("mqtt_is_connected"))


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: SemsConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up station binary sensors."""
    async_add_entities(
        [SemsMqttConnectedBinarySensor(config_entry.runtime_data.coordinator)]
    )
