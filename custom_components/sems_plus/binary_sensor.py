"""Binary sensors for the GoodWe SEMS+ integration."""

from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import SemsPlusConfigEntry
from .entity import SemsPlusDeviceEntity, SemsPlusStationEntity
from .mapping import (
    DEVICE_BINARY_SENSORS,
    STATION_BINARY_SENSORS,
    SemsDeviceBinarySensorDescription,
    SemsStationBinarySensorDescription,
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SemsPlusConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up station and device binary sensors."""
    coordinator = entry.runtime_data.coordinator
    entities: list[BinarySensorEntity] = [
        SemsPlusStationBinarySensor(coordinator, description)
        for description in STATION_BINARY_SENSORS
    ]
    for device in coordinator.data.devices.values():
        entities.extend(
            SemsPlusDeviceBinarySensor(coordinator, device, description)
            for description in DEVICE_BINARY_SENSORS.get(device.device_type, ())
        )
    async_add_entities(entities)


class SemsPlusStationBinarySensor(SemsPlusStationEntity, BinarySensorEntity):
    """A binary sensor on the station device."""

    entity_description: SemsStationBinarySensorDescription

    @property
    def is_on(self) -> bool | None:
        """Return the state."""
        description = self.entity_description
        if description.client_fn is not None:
            return description.client_fn(self.coordinator.client)
        if description.value_fn is not None:
            return description.value_fn(self.coordinator.data)
        return None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the value source."""
        return self._source_attributes()


class SemsPlusDeviceBinarySensor(SemsPlusDeviceEntity, BinarySensorEntity):
    """A binary sensor on one SEMS device."""

    entity_description: SemsDeviceBinarySensorDescription

    @property
    def is_on(self) -> bool | None:
        """Return the state."""
        device = self.device
        if device is None:
            return None
        return self.entity_description.value_fn(self.coordinator.data, device)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the value source."""
        return self._source_attributes()
