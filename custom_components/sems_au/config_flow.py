"""Config flow for sems integration."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import selector

from .const import (
    CONF_REGION,
    CONF_STATION_ID,
    DEFAULT_SEMS_REGION,
    DOMAIN,
    SEMS_REGIONS,
    redact_for_log,
)
from .sems_api import SemsApi, SemsStation

_LOGGER = logging.getLogger(__name__)

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_USERNAME): str,
        vol.Required(CONF_PASSWORD): str,
        vol.Required(CONF_REGION, default=DEFAULT_SEMS_REGION): selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=list(SEMS_REGIONS),
                mode=selector.SelectSelectorMode.DROPDOWN,
            )
        ),
    }
)


async def validate_credentials(hass: HomeAssistant, data: dict[str, Any]) -> SemsApi:
    """Validate credentials and return an authenticated API client."""
    _LOGGER.debug(
        "SEMS - Validating credentials for user: %s",
        redact_for_log(data.get(CONF_USERNAME, "")),
    )
    api = SemsApi(
        hass,
        data[CONF_USERNAME],
        data[CONF_PASSWORD],
        data.get(CONF_REGION, DEFAULT_SEMS_REGION),
    )
    authenticated = await hass.async_add_executor_job(api.test_authentication)
    if not authenticated:
        raise InvalidAuth
    return api


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for sems."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the config flow."""
        self._credentials: dict[str, Any] = {}
        self._stations: dict[str, SemsStation] = {}
        self._station_step_error: str | None = None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Handle the initial step."""
        if user_input is None:
            return self.async_show_form(
                step_id="user", data_schema=STEP_USER_DATA_SCHEMA
            )

        errors: dict[str, str] = {}

        try:
            api = await validate_credentials(self.hass, user_input)
            _LOGGER.debug("SEMS - Credentials valid, fetching stations")
            stations = await self.hass.async_add_executor_job(api.get_stations)
            configured_station_ids = {
                entry.unique_id or entry.data.get(CONF_STATION_ID)
                for entry in self.hass.config_entries.async_entries(DOMAIN)
            }

            self._credentials = dict(user_input)
            self._stations = {
                station.station_id: station
                for station in stations
                if station.station_id not in configured_station_ids
            }
            if not stations:
                self._station_step_error = "no_stations_found"
            elif not self._stations:
                self._station_step_error = "all_stations_configured"
            else:
                self._station_step_error = None
            return await self.async_step_station()

        except CannotConnect:
            errors["base"] = "cannot_connect"
        except InvalidAuth:
            errors["base"] = "invalid_auth"
        except Exception:  # pylint: disable=broad-except
            _LOGGER.exception("Unexpected exception")
            errors["base"] = "unknown"

        return self.async_show_form(
            step_id="user", data_schema=STEP_USER_DATA_SCHEMA, errors=errors
        )

    async def async_step_station(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Ask the user to select one unconfigured station."""
        errors: dict[str, str] = {}
        if user_input is not None:
            station_id = user_input.get(CONF_STATION_ID)
            station = (
                self._stations.get(station_id) if isinstance(station_id, str) else None
            )
            if station is None:
                errors["base"] = "invalid_station"
            else:
                await self.async_set_unique_id(station.station_id)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=station.name,
                    data={
                        **self._credentials,
                        CONF_STATION_ID: station.station_id,
                        "station_name": station.name,
                    },
                )

        if self._station_step_error is not None:
            errors["base"] = self._station_step_error

        options: list[selector.SelectOptionDict] = [
            {"value": station.station_id, "label": station.name}
            for station in sorted(self._stations.values(), key=lambda item: item.name)
        ]
        schema = (
            vol.Schema(
                {
                    vol.Required(CONF_STATION_ID): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=options,
                            mode=selector.SelectSelectorMode.DROPDOWN,
                        )
                    )
                }
            )
            if options
            else vol.Schema({})
        )
        return self.async_show_form(
            step_id="station",
            data_schema=schema,
            errors=errors,
        )


class CannotConnect(HomeAssistantError):
    """Error to indicate we cannot connect."""


class InvalidAuth(HomeAssistantError):
    """Error to indicate there is invalid auth."""
