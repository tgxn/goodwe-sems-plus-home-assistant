"""Sensors for the GoodWe SEMS+ integration."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import DeviceData, SemsPlusConfigEntry, SemsPlusCoordinator
from .entity import SemsPlusDeviceEntity, SemsPlusStationEntity
from .mapping import (
    STATION_SENSORS,
    SemsDeviceSensorDescription,
    SemsStationSensorDescription,
    device_sensor_descriptions,
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SemsPlusConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up station and device sensors."""
    coordinator = entry.runtime_data.coordinator
    entities: list[SensorEntity] = [
        SemsPlusStationSensor(coordinator, description)
        for description in STATION_SENSORS
    ]
    for device in coordinator.data.devices.values():
        entities.extend(
            SemsPlusDeviceSensor(coordinator, device, description)
            for description in device_sensor_descriptions(device)
        )
    async_add_entities(entities)


class SemsPlusStationSensor(SemsPlusStationEntity, SensorEntity):
    """A whole-system sensor on the station device."""

    entity_description: SemsStationSensorDescription

    @property
    def native_value(self) -> Any:
        """Return the sensor value."""
        description = self.entity_description
        if description.client_fn is not None:
            return description.client_fn(self.coordinator.client)
        if description.value_fn is not None:
            return description.value_fn(self.coordinator.data)
        return None

    @property
    def native_unit_of_measurement(self) -> str | None:
        """Return the unit, which can depend on the data (e.g. currency)."""
        if self.entity_description.unit_fn is not None:
            return self.entity_description.unit_fn(self.coordinator.data)
        return super().native_unit_of_measurement

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the value source (and raw status code)."""
        code_fn = self.entity_description.code_fn
        return self._source_attributes(
            code_fn(self.coordinator.data) if code_fn else None
        )


class SemsPlusDeviceSensor(SemsPlusDeviceEntity, SensorEntity):
    """A sensor on one SEMS device."""

    entity_description: SemsDeviceSensorDescription

    def __init__(
        self,
        coordinator: SemsPlusCoordinator,
        device: DeviceData,
        description: SemsDeviceSensorDescription,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, device, description)
        if description.placeholders:
            self._attr_translation_placeholders = description.placeholders

    @property
    def native_value(self) -> Any:
        """Return the sensor value."""
        device = self.device
        if device is None:
            return None
        return self.entity_description.value_fn(self.coordinator.data, device)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the value source (and raw status code)."""
        code_fn = self.entity_description.code_fn
        device = self.device
        code = code_fn(self.coordinator.data, device) if code_fn and device else None
        return self._source_attributes(code)
