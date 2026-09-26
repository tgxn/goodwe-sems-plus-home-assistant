"""Tests for the SEMS+ value mapping helpers."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from custom_components.sems_plus.coordinator import DeviceData, SemsPlusData
from custom_components.sems_plus.mapping import (
    DEVICE_STATUS,
    DEVICE_TYPE_BATTERY,
    DEVICE_TYPE_INVERTER,
    STATION_STATUS,
    battery_state,
    device_sensor_descriptions,
    device_sw_version,
    flatten_factors,
    grid_state,
    kw_to_w,
    mv_to_v,
    parse_flow_live,
    percentage,
    signed_power,
    split_negative,
    split_positive,
    status_label,
    summarize_statistics,
)

from .conftest import load_fixture


def inverter() -> DeviceData:
    """Return the captured inverter as DeviceData."""
    return DeviceData(
        sn="INV0000000000001",
        device_type=DEVICE_TYPE_INVERTER,
        meta={},
        factors=flatten_factors(
            load_fixture("inverter_telecounting.json"),
            load_fixture("inverter_telemetry.json"),
        ),
        info={
            code: factor.value
            for code, factor in flatten_factors(
                load_fixture("inverter_information.json")
            ).items()
        },
    )


def battery(name: str) -> DeviceData:
    """Return a captured battery rack as DeviceData."""
    return DeviceData(
        sn=name,
        device_type=DEVICE_TYPE_BATTERY,
        meta={},
        factors=flatten_factors(
            load_fixture(f"{name}_telecounting.json"),
            load_fixture(f"{name}_telemetry.json"),
        ),
    )


def test_flatten_factors_prefers_later_source() -> None:
    """Telemetry (passed last) wins over telecounting for shared codes."""
    factors = flatten_factors(
        [
            {
                "code": "g",
                "factors": [{"code": "pAc", "data": "1", "dataType": "NUMERIC"}],
            }
        ],
        [
            {
                "code": "g",
                "factors": [{"code": "pAc", "data": "2", "dataType": "NUMERIC"}],
            }
        ],
    )
    assert factors["pAc"].value == Decimal(2)


def test_flatten_factors_real_inverter() -> None:
    """Captured inverter factors are flattened with units and types."""
    device = inverter()
    assert device.factors["Vac"].unit == "V"
    assert isinstance(device.factors["Vac"].value, Decimal)
    assert device.factors["sn"].numeric is False
    assert device.factors["proPvStatsToday"].value == Decimal(10)
    assert device.info["safetyVersion"] == "1.0.0"


def test_flow_direction_and_signs() -> None:
    """Flows give HA signs: battery +discharge/-charge, grid +import/-export."""
    discharging = {"pBat": ["pConsum"]}
    charging_exporting = {"pSystem": ["pBat", "pGrid", "pConsum"]}
    importing = {"pGrid": ["pConsum"]}

    assert battery_state(discharging) == "discharging"
    assert battery_state(charging_exporting) == "charging"
    assert battery_state({}) == "idle"
    assert grid_state(importing) == "importing"
    assert grid_state(charging_exporting) == "exporting"
    assert grid_state(discharging) == "idle"

    assert signed_power(Decimal(500), "discharging") == Decimal(500)
    assert signed_power(Decimal(500), "charging") == Decimal(-500)
    assert signed_power(Decimal(500), "exporting") == Decimal(-500)
    assert signed_power(Decimal(-20), "discharging") == Decimal(-20)
    assert split_positive(Decimal(-500)) == 0
    assert split_negative(Decimal(-500)) == Decimal(500)
    assert split_positive(None) is None


def test_unit_conversion() -> None:
    """kW → W and mV → V."""
    assert kw_to_w("0.25") == Decimal(250)
    assert mv_to_v("3291") == Decimal("3.291")
    assert kw_to_w("") is None


def test_statistics_today_and_month() -> None:
    """Today is the matching date; the month is the sum of every day."""
    response = {
        "dataList": [
            {
                "item": "proPurchaseStats",
                "statisticsList": [
                    {"date": "2026-09-22", "val": 1.5},
                    {"date": "2026-09-23", "val": 0.1},
                ],
            }
        ]
    }
    today, month = summarize_statistics(response, date(2026, 9, 23))
    assert today == {"proPurchaseStats": Decimal("0.1")}
    assert month == {"proPurchaseStats": Decimal("1.6")}

    today, _ = summarize_statistics(
        load_fixture("statistics_today.json"), date(2026, 9, 23)
    )
    assert today["proGridStats"] == Decimal(3)


def test_status_labels() -> None:
    """Known codes map to labels; unknown codes report unknown."""
    assert status_label(STATION_STATUS, "1") == "working"
    assert status_label(DEVICE_STATUS, 5) == "running"
    assert status_label(DEVICE_STATUS, 9) == "unknown"
    assert status_label(DEVICE_STATUS, None) is None


def test_percentage_is_clamped() -> None:
    """Rates stay within 0-100 and need a positive whole."""
    assert percentage(Decimal("12.9"), Decimal(13)) == Decimal("12.9") / 13 * 100
    assert percentage(Decimal(20), Decimal(10)) == 100
    assert percentage(Decimal(1), Decimal(0)) is None


def test_inverter_descriptions() -> None:
    """MPPT strings are detected and every known code is handled."""
    keys = {description.key for description in device_sensor_descriptions(inverter())}
    assert {"pv1_power", "pv2_voltage", "grid_voltage", "status"} <= keys
    assert not any(key.startswith("extra_") for key in keys)


def test_unknown_factor_becomes_disabled_extra() -> None:
    """New numeric codes surface as disabled sensors named from the alias."""
    device = battery("battery_1")
    device.factors.update(
        flatten_factors(
            [
                {
                    "code": "newThing",
                    "data": "1.5",
                    "dataType": "NUMERIC",
                    "unit": "kW",
                    "alias": "new_thing",
                }
            ]
        )
    )
    extras = [
        description
        for description in device_sensor_descriptions(device)
        if description.key.startswith("extra_")
    ]
    assert len(extras) == 1
    assert extras[0].name == "New thing (newThing)"
    assert extras[0].entity_registry_enabled_default is False
    assert extras[0].value_fn(SemsPlusData(station_id="s"), device) == Decimal(1500)


def test_firmware_versions() -> None:
    """Firmware comes from information (inverter) or BMS telemetry (battery)."""
    assert device_sw_version(inverter()) == "1.0.0"
    assert device_sw_version(battery("battery_1")) == "BMS 1.0, DCDC 1.0"


def test_parse_flow_live() -> None:
    """REST flow values stay in kW (converted by entities) with flows kept."""
    values, flows = parse_flow_live(load_fixture("station_flow.json"))
    assert values["pBat"] == Decimal("0.5")
    assert flows == {"pBat": ["pConsum"]}
