"""Tests for the SEMS config flow."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.sems_au.const import (
    CONF_REGION,
    CONF_STATION_ID,
    DEFAULT_SEMS_REGION,
    DOMAIN,
)
from custom_components.sems_au.sems_api import SemsStation

MOCK_USERNAME = "test@example.com"
MOCK_PASSWORD = "test_password"
MOCK_STATION_ID_1 = "12345678-1234-5678-9abc-123456789abc"
MOCK_STATION_ID_2 = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


@pytest.fixture
def mock_setup_entry():
    """Prevent the integration from being set up during config flow tests."""
    with patch(
        "custom_components.sems_au.async_setup_entry", return_value=True
    ) as mock:
        yield mock


async def _submit_credentials(hass: HomeAssistant, stations: list[SemsStation]) -> dict:
    """Start setup and submit valid account credentials."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "user"

    with (
        patch(
            "custom_components.sems_au.sems_api.SemsApi.test_authentication",
            return_value=True,
        ),
        patch(
            "custom_components.sems_au.sems_api.SemsApi.get_stations",
            return_value=stations,
        ),
    ):
        return await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_USERNAME: MOCK_USERNAME, CONF_PASSWORD: MOCK_PASSWORD},
        )


async def test_single_station_still_requires_selection(
    hass: HomeAssistant,
    enable_custom_integrations: None,
    mock_setup_entry,
) -> None:
    """A single station should still be explicitly selected."""
    del enable_custom_integrations
    result = await _submit_credentials(hass, [SemsStation(MOCK_STATION_ID_1, "Home")])

    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "station"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_STATION_ID: MOCK_STATION_ID_1}
    )

    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["title"] == "Home"
    assert result["data"] == {
        CONF_USERNAME: MOCK_USERNAME,
        CONF_PASSWORD: MOCK_PASSWORD,
        CONF_REGION: DEFAULT_SEMS_REGION,
        CONF_STATION_ID: MOCK_STATION_ID_1,
        "station_name": "Home",
    }


async def test_station_step_lists_multiple_stations(
    hass: HomeAssistant,
    enable_custom_integrations: None,
    mock_setup_entry,
) -> None:
    """Station selection should list every unconfigured station."""
    del enable_custom_integrations
    result = await _submit_credentials(
        hass,
        [
            SemsStation(MOCK_STATION_ID_2, "Workshop"),
            SemsStation(MOCK_STATION_ID_1, "Home"),
        ],
    )

    options = result["data_schema"].schema[CONF_STATION_ID].config["options"]
    assert options == [
        {"value": MOCK_STATION_ID_1, "label": "Home"},
        {"value": MOCK_STATION_ID_2, "label": "Workshop"},
    ]


async def test_station_step_filters_configured_stations(
    hass: HomeAssistant,
    enable_custom_integrations: None,
    mock_setup_entry,
) -> None:
    """Previously configured stations should not be selectable."""
    del enable_custom_integrations
    MockConfigEntry(
        domain=DOMAIN,
        unique_id=MOCK_STATION_ID_1,
        data={CONF_STATION_ID: MOCK_STATION_ID_1},
    ).add_to_hass(hass)

    result = await _submit_credentials(
        hass,
        [
            SemsStation(MOCK_STATION_ID_1, "Home"),
            SemsStation(MOCK_STATION_ID_2, "Workshop"),
        ],
    )

    options = result["data_schema"].schema[CONF_STATION_ID].config["options"]
    assert options == [{"value": MOCK_STATION_ID_2, "label": "Workshop"}]


@pytest.mark.parametrize(
    ("stations", "expected_error"),
    [
        ([], "no_stations_found"),
        ([SemsStation(MOCK_STATION_ID_1, "Home")], "all_stations_configured"),
    ],
)
async def test_station_step_shows_empty_states(
    hass: HomeAssistant,
    enable_custom_integrations: None,
    mock_setup_entry,
    stations: list[SemsStation],
    expected_error: str,
) -> None:
    """Empty discovery results should remain on the station step."""
    del enable_custom_integrations
    if stations:
        MockConfigEntry(
            domain=DOMAIN,
            unique_id=MOCK_STATION_ID_1,
            data={CONF_STATION_ID: MOCK_STATION_ID_1},
        ).add_to_hass(hass)

    result = await _submit_credentials(hass, stations)

    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "station"
    assert result["errors"]["base"] == expected_error
    assert result["data_schema"].schema == {}


async def test_invalid_authentication_stays_on_user_step(
    hass: HomeAssistant,
    enable_custom_integrations: None,
) -> None:
    """Invalid credentials should not advance to station discovery."""
    del enable_custom_integrations
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )

    with patch(
        "custom_components.sems_au.sems_api.SemsApi.test_authentication",
        return_value=False,
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_USERNAME: MOCK_USERNAME, CONF_PASSWORD: "wrong"},
        )

    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"]["base"] == "invalid_auth"
