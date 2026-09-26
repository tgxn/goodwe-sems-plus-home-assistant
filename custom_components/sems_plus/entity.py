"""Base entities for the SEMS+ integration."""

from __future__ import annotations

from typing import Any

from homeassistant.helpers.entity import EntityDescription
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .coordinator import DeviceData, SemsPlusCoordinator
from .device import device_info_for_device, device_info_for_station


class SemsPlusStationEntity(CoordinatorEntity[SemsPlusCoordinator]):
    """Entity on the station device (whole-system values)."""

    _attr_has_entity_name = True

    def __init__(
        self, coordinator: SemsPlusCoordinator, description: EntityDescription
    ) -> None:
        """Initialize the entity."""
        super().__init__(coordinator)
        self.entity_description = description
        station_id = coordinator.station_id
        self._attr_unique_id = f"{station_id}-{description.key}"
        self._attr_device_info = device_info_for_station(
            coordinator.data, coordinator.client.region.web_origin
        )

    def _source_attributes(self, code: Any = None) -> dict[str, Any]:
        attributes: dict[str, Any] = {
            "source": getattr(self.entity_description, "source", None)
        }
        if code is not None:
            attributes["code"] = code
        return attributes


class SemsPlusDeviceEntity(CoordinatorEntity[SemsPlusCoordinator]):
    """Entity on one SEMS device (inverter, battery rack, dongle)."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: SemsPlusCoordinator,
        device: DeviceData,
        description: EntityDescription,
    ) -> None:
        """Initialize the entity."""
        super().__init__(coordinator)
        self.entity_description = description
        self._sn = device.sn
        self._attr_unique_id = f"{coordinator.station_id}-{device.sn}-{description.key}"
        self._attr_device_info = device_info_for_device(
            coordinator.station_device_id, device
        )

    @property
    def device(self) -> DeviceData | None:
        """Return this entity's device data, if the device is still reported."""
        return self.coordinator.data.devices.get(self._sn)

    @property
    def available(self) -> bool:
        """Return whether the device is still reported by SEMS."""
        return super().available and self.device is not None

    def _source_attributes(self, code: Any = None) -> dict[str, Any]:
        attributes: dict[str, Any] = {
            "source": getattr(self.entity_description, "source", None)
        }
        if code is not None:
            attributes["code"] = code
        return attributes
