"""Config flow for the GoodWe SEMS+ integration."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    CONF_FALLBACK_INTERVAL,
    CONF_METADATA_INTERVAL,
    CONF_POLL_INTERVAL,
    CONF_REGION,
    CONF_STATION_ID,
    DEFAULT_FALLBACK_INTERVAL,
    DEFAULT_METADATA_INTERVAL,
    DEFAULT_POLL_INTERVAL,
    DEFAULT_SEMS_REGION,
    DOMAIN,
    MIN_FALLBACK_INTERVAL,
    MIN_METADATA_INTERVAL,
    MIN_POLL_INTERVAL,
    SEMS_REGIONS,
    redact_for_log,
)
from .coordinator import SemsPlusConfigEntry
from .mapping import DEVICE_TYPE_LABELS
from .sems_api_v2 import SemsApiError, SemsAuthError, SemsPlusClient

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


def _interval_selector(minimum: int) -> selector.NumberSelector:
    return selector.NumberSelector(
        selector.NumberSelectorConfig(
            min=minimum,
            max=86400,
            step=1,
            unit_of_measurement="s",
            mode=selector.NumberSelectorMode.BOX,
        )
    )


def interval_schema(values: Mapping[str, Any]) -> vol.Schema:
    """Return the update-interval form, defaulting to `values`."""
    return vol.Schema(
        {
            vol.Required(
                CONF_POLL_INTERVAL,
                default=values.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL),
            ): _interval_selector(MIN_POLL_INTERVAL),
            vol.Required(
                CONF_FALLBACK_INTERVAL,
                default=values.get(CONF_FALLBACK_INTERVAL, DEFAULT_FALLBACK_INTERVAL),
            ): _interval_selector(MIN_FALLBACK_INTERVAL),
            vol.Required(
                CONF_METADATA_INTERVAL,
                default=values.get(CONF_METADATA_INTERVAL, DEFAULT_METADATA_INTERVAL),
            ): _interval_selector(MIN_METADATA_INTERVAL),
        }
    )


def _intervals(user_input: Mapping[str, Any]) -> dict[str, int]:
    return {
        key: int(user_input[key])
        for key in (CONF_POLL_INTERVAL, CONF_FALLBACK_INTERVAL, CONF_METADATA_INTERVAL)
    }


def _client(hass: HomeAssistant, data: Mapping[str, Any]) -> SemsPlusClient:
    return SemsPlusClient(
        async_get_clientsession(hass),
        data[CONF_USERNAME],
        data[CONF_PASSWORD],
        SEMS_REGIONS[data.get(CONF_REGION, DEFAULT_SEMS_REGION)],
    )


def _device_label(row: Mapping[str, Any]) -> str:
    name = row.get("name") or row.get("sn") or "Device"
    kind = DEVICE_TYPE_LABELS.get(str(row.get("deviceType")), row.get("deviceType"))
    model = row.get("model")
    details = ", ".join(str(part) for part in (kind, model) if part)
    return f"- {name} ({details})" if details else f"- {name}"


class SemsPlusConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for GoodWe SEMS+."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the config flow."""
        self._credentials: dict[str, Any] = {}
        self._client: SemsPlusClient | None = None
        self._stations: dict[str, str] = {}
        self._station_error: str | None = None
        self._station_id: str | None = None
        self._devices: list[dict[str, Any]] = []

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: SemsPlusConfigEntry,
    ) -> SemsPlusOptionsFlow:
        """Return the options flow."""
        return SemsPlusOptionsFlow()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for credentials and list the account's stations."""
        errors: dict[str, str] = {}
        if user_input is not None:
            _LOGGER.debug(
                "SEMS+ - Validating credentials for %s",
                redact_for_log(user_input[CONF_USERNAME]),
            )
            client = _client(self.hass, user_input)
            try:
                await client.login()
                stations = await client.get_stations()
            except SemsAuthError:
                errors["base"] = "invalid_auth"
            except SemsApiError:
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unexpected exception")
                errors["base"] = "unknown"
            else:
                configured = {
                    entry.unique_id for entry in self._async_current_entries()
                }
                self._credentials = dict(user_input)
                self._client = client
                self._stations = {
                    row["id"]: str(row.get("name") or row["id"])
                    for row in stations
                    if isinstance(row.get("id"), str) and row["id"] not in configured
                }
                if not stations:
                    self._station_error = "no_stations_found"
                elif not self._stations:
                    self._station_error = "all_stations_configured"
                else:
                    self._station_error = None
                return await self.async_step_station()

        return self.async_show_form(
            step_id="user", data_schema=STEP_USER_DATA_SCHEMA, errors=errors
        )

    async def async_step_station(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Select one unconfigured station."""
        errors: dict[str, str] = {}
        if user_input is not None:
            station_id = user_input.get(CONF_STATION_ID)
            if station_id not in self._stations:
                errors["base"] = "invalid_station"
            else:
                await self.async_set_unique_id(station_id)
                self._abort_if_unique_id_configured()
                self._station_id = station_id
                assert self._client is not None
                try:
                    self._devices = await self._client.get_station_devices(station_id)
                except SemsApiError:
                    errors["base"] = "cannot_connect"
                else:
                    return await self.async_step_settings()

        if self._station_error is not None:
            errors["base"] = self._station_error

        options = [
            selector.SelectOptionDict(value=station_id, label=name)
            for station_id, name in sorted(self._stations.items(), key=lambda i: i[1])
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
            step_id="station", data_schema=schema, errors=errors
        )

    async def async_step_settings(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show the devices that will be added and ask for update intervals."""
        assert self._station_id is not None
        station_name = self._stations[self._station_id]
        if user_input is not None:
            return self.async_create_entry(
                title=station_name,
                data={
                    **self._credentials,
                    CONF_STATION_ID: self._station_id,
                },
                options=_intervals(user_input),
            )

        devices = "\n".join(_device_label(row) for row in self._devices) or "-"
        return self.async_show_form(
            step_id="settings",
            data_schema=interval_schema({}),
            description_placeholders={"station": station_name, "devices": devices},
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Start reauthentication after SEMS+ rejects the stored credentials."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for a new password."""
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            data = {**entry.data, CONF_PASSWORD: user_input[CONF_PASSWORD]}
            try:
                await _client(self.hass, data).login()
            except SemsAuthError:
                errors["base"] = "invalid_auth"
            except SemsApiError:
                errors["base"] = "cannot_connect"
            else:
                return self.async_update_reload_and_abort(entry, data=data)

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_PASSWORD): str}),
            description_placeholders={"username": entry.data[CONF_USERNAME]},
            errors=errors,
        )


class SemsPlusOptionsFlow(OptionsFlowWithReload):
    """Edit update intervals; the entry reloads when they change."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show the update-interval form."""
        if user_input is not None:
            return self.async_create_entry(data=_intervals(user_input))
        return self.async_show_form(
            step_id="init", data_schema=interval_schema(self.config_entry.options)
        )
