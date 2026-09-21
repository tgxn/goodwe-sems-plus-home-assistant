"""Tests for SEMS device helpers."""

from custom_components.sems_au.device import (
    device_info_for_battery,
    device_info_for_inverter,
    device_info_for_station,
)


def test_station_device_has_no_inverter_identity() -> None:
    """The station should not inherit model or firmware from an inverter."""
    device_info = device_info_for_station(
        "station-123",
        "Test Solar Farm",
        {"powerstation_type": "Battery Storage"},
    )

    assert device_info["identifiers"] == {("sems_au", "station-123")}
    assert device_info["name"] == "Test Solar Farm"
    assert device_info["model"] == "Battery Storage"
    assert "sw_version" not in device_info
    assert "via_device" not in device_info


def test_inverter_device_is_child_of_station() -> None:
    """Inverter identity and firmware should belong to the inverter child."""
    device_info = device_info_for_inverter(
        "station-123",
        "inverter-456",
        {
            "name": "All-in-One 1",
            "model_type": "GW5K-EHA-G20",
            "firmwareversion": 7.0,
        },
    )

    assert device_info["identifiers"] == {
        ("sems_au", "station-123:inverter:inverter-456")
    }
    assert device_info["name"] == "All-in-One 1"
    assert device_info["model"] == "GW5K-EHA-G20"
    assert device_info["sw_version"] == "7.0"
    assert device_info["via_device"] == ("sems_au", "station-123")


def test_battery_device_is_child_of_station() -> None:
    """A BMS serial should identify a separate battery child device."""
    device_info = device_info_for_battery(
        "station-123",
        "battery-789",
        {"name": "Battery 1", "bmssoftwareversion": 1544.0},
    )

    assert device_info["identifiers"] == {
        ("sems_au", "station-123:battery:battery-789")
    }
    assert device_info["name"] == "Battery 1"
    assert device_info["model"] == "Battery"
    assert device_info["sw_version"] == "1544.0"
    assert device_info["via_device"] == ("sems_au", "station-123")
