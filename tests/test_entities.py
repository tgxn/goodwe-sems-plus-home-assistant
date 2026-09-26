"""Entity naming and translation rules for SEMS+."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.sems_plus.const import DOMAIN
from custom_components.sems_plus.mapping import (
    DEVICE_BINARY_SENSORS,
    DEVICE_SENSORS,
    STATION_BINARY_SENSORS,
    STATION_SENSORS,
)

from .conftest import CAPTURE_TIME, STATION_ID

COMPONENT = Path(__file__).parents[1] / "custom_components" / "sems_plus"

pytestmark = pytest.mark.freeze_time(CAPTURE_TIME)


def _descriptions():
    yield from (("sensor", d) for d in STATION_SENSORS)
    yield from (("binary_sensor", d) for d in STATION_BINARY_SENSORS)
    for descriptions in DEVICE_SENSORS.values():
        yield from (("sensor", d) for d in descriptions)
    for descriptions in DEVICE_BINARY_SENSORS.values():
        yield from (("binary_sensor", d) for d in descriptions)


def test_every_translation_key_has_english_name() -> None:
    """Each mapped entity has a name (and enum state labels) in strings.json."""
    strings = json.loads((COMPONENT / "strings.json").read_text())["entity"]
    english = json.loads((COMPONENT / "translations" / "en.json").read_text())["entity"]
    assert strings.keys() == english.keys()
    keys = {
        ("sensor", k)
        for k in ("pv_string_power", "pv_string_voltage", "pv_string_current")
    }
    for platform, description in _descriptions():
        keys.add((platform, description.translation_key))
        options = getattr(description, "options", None)
        for option in options or []:
            assert option in strings[platform][description.translation_key]["state"]
    for platform, key in keys:
        assert strings[platform][key]["name"], key
        assert english[platform][key]["name"] == strings[platform][key]["name"]


async def test_entities_are_on_their_own_devices(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_client: dict[str, MagicMock],
) -> None:
    """Entity names are unique per device and each entity sits on its source device."""
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    devices = dr.async_get(hass)
    entities = er.async_entries_for_config_entry(
        er.async_get(hass), config_entry.entry_id
    )
    names_by_device: dict[str, set[tuple[str, str]]] = {}

    for entry in entities:
        device = devices.async_get(entry.device_id)
        assert device is not None
        (identifier,) = {
            value for domain, value in device.identifiers if domain == DOMAIN
        }
        suffix = entry.unique_id.removeprefix(f"{STATION_ID}-")
        if identifier == STATION_ID:
            assert "-" not in suffix, (
                f"{entry.unique_id} is a device value on the station"
            )
        else:
            assert suffix.startswith(f"{identifier}-"), entry.unique_id

        name = entry.original_name or entry.translation_key
        assert name is not None
        if name.startswith("pv_string_"):
            # One translation key per MPPT measurement; the string number differs.
            name = f"{name}|{suffix.split('-')[-1].split('_')[0]}"
        key = (entry.domain, name)
        seen = names_by_device.setdefault(identifier, set())
        assert key not in seen, f"duplicate name {key} on {device.name}"
        seen.add(key)

    # Friendly names of every enabled entity are unique across the station.
    friendly = [
        state.attributes["friendly_name"]
        for state in hass.states.async_all()
        if state.entity_id.split(".")[0] in ("sensor", "binary_sensor")
    ]
    assert len(friendly) == len(set(friendly))
