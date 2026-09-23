"""Update coordinator for the SEMS+ integration.

Three task runners keep the data current:

- the coordinator poll (`poll_interval`): station totals, device list and
  per-device telemetry, plus metadata every `metadata_interval`
- the client's MQTT live feed: pushes station power every few seconds
- a REST fallback timer (`fallback_interval`): polls stations/flow only while
  the live feed is not connected
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import (
    CONF_FALLBACK_INTERVAL,
    CONF_METADATA_INTERVAL,
    CONF_POLL_INTERVAL,
    CONF_STATION_ID,
    DEFAULT_FALLBACK_INTERVAL,
    DEFAULT_METADATA_INTERVAL,
    DEFAULT_POLL_INTERVAL,
    DOMAIN,
    redact_for_log,
)
from .mapping import (
    DEVICE_TYPE_INVERTER,
    Factor,
    flatten_factors,
    parse_flow_live,
    summarize_statistics,
)
from .sems_api_v2 import (
    SemsApiError,
    SemsAuthError,
    SemsLiveData,
    SemsPlusClient,
    SemsRateLimitedError,
)

_LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class DeviceData:
    """Latest data for one SEMS device."""

    sn: str
    device_type: str
    meta: dict[str, Any]
    factors: dict[str, Factor] = field(default_factory=dict)
    info: dict[str, Any] = field(default_factory=dict)
    related: list[dict[str, Any]] = field(default_factory=list)

    @property
    def name(self) -> str:
        """Return the device name from SEMS."""
        name = self.meta.get("name")
        return name if isinstance(name, str) and name else self.sn


@dataclass(slots=True)
class SemsPlusData:
    """Coordinator data for one station."""

    station_id: str
    station: dict[str, Any] = field(default_factory=dict)
    basic: dict[str, Any] = field(default_factory=dict)
    production: dict[str, Any] = field(default_factory=dict)
    stats_today: dict[str, Decimal] = field(default_factory=dict)
    stats_month: dict[str, Decimal] = field(default_factory=dict)
    live: dict[str, Decimal] = field(default_factory=dict)
    flows: dict[str, list[str]] = field(default_factory=dict)
    live_at: datetime | None = None
    live_source: str | None = None
    devices: dict[str, DeviceData] = field(default_factory=dict)

    @property
    def station_name(self) -> str:
        """Return the station name from SEMS."""
        name = self.station.get("name") or self.basic.get("name")
        return name if isinstance(name, str) and name else "SEMS+ Station"


type SemsPlusConfigEntry = ConfigEntry[SemsPlusRuntimeData]


@dataclass(slots=True)
class SemsPlusRuntimeData:
    """Runtime objects stored on the config entry."""

    client: SemsPlusClient
    coordinator: SemsPlusCoordinator


class SemsPlusCoordinator(DataUpdateCoordinator[SemsPlusData]):
    """Fetch SEMS+ data and merge the live feed."""

    config_entry: SemsPlusConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: SemsPlusConfigEntry,
        client: SemsPlusClient,
    ) -> None:
        """Initialize the coordinator."""
        options = entry.options
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=timedelta(
                seconds=options.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL)
            ),
        )
        self.client = client
        self.station_id: str = entry.data[CONF_STATION_ID]
        self._fallback_interval = timedelta(
            seconds=options.get(CONF_FALLBACK_INTERVAL, DEFAULT_FALLBACK_INTERVAL)
        )
        self._metadata_interval = timedelta(
            seconds=options.get(CONF_METADATA_INTERVAL, DEFAULT_METADATA_INTERVAL)
        )
        self._metadata_at: datetime | None = None
        self._basic: dict[str, Any] = {}
        self._info: dict[str, dict[str, Any]] = {}
        self._live: dict[str, Decimal] = {}
        self._flows: dict[str, list[str]] = {}
        self._live_at: datetime | None = None
        self._live_source: str | None = None
        self._fallback_unsub: Callable[[], None] | None = None
        # Device registry id of the station device, set during entry setup.
        self.station_device_id: str | None = None

    # ------------------------------------------------------------------
    # Poll
    # ------------------------------------------------------------------

    def _station_today(self, devices: list[dict[str, Any]]) -> date:
        """Return today's date in the station's timezone."""
        zone = self._basic.get("zoneId") or next(
            (row.get("zoneId") for row in devices if row.get("zoneId")), None
        )
        timezone = dt_util.get_time_zone(zone) if isinstance(zone, str) else None
        return dt_util.now(timezone).date()

    async def _async_update_data(self) -> SemsPlusData:
        """Fetch everything except the live feed."""
        try:
            return await self._async_fetch()
        except SemsAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except SemsRateLimitedError as err:
            raise UpdateFailed(
                f"SEMS+ API rate limited (retry after {err.retry_after}s)"
            ) from err
        except SemsApiError as err:
            raise UpdateFailed(f"Error communicating with SEMS+: {err}") from err

    async def _async_fetch(self) -> SemsPlusData:
        client = self.client
        station_id = self.station_id

        stations, device_rows = await asyncio.gather(
            client.get_stations(), client.get_station_devices(station_id)
        )
        station = next((row for row in stations if row.get("id") == station_id), {})
        today = self._station_today(device_rows)

        now = dt_util.utcnow()
        refresh_metadata = (
            self._metadata_at is None
            or now - self._metadata_at >= self._metadata_interval
        )
        if refresh_metadata:
            await self._async_fetch_metadata(device_rows)
            self._metadata_at = now
            today = self._station_today(device_rows)

        statistics, production = await asyncio.gather(
            client.get_station_statistics(station_id, today.replace(day=1), today),
            client.get_station_production(station_id, today),
        )
        stats_today, stats_month = summarize_statistics(statistics, today)
        if not stats_today and today.day != 1:
            # Month range did not include per-day entries; ask for today only.
            stats_today, _ = summarize_statistics(
                await client.get_station_statistics(station_id, today, today), today
            )

        devices = await asyncio.gather(
            *(self._async_fetch_device(row) for row in device_rows if row.get("sn"))
        )

        if self.client.mqtt_state != "connected":
            await self._async_merge_flow()

        data = SemsPlusData(
            station_id=station_id,
            station=station,
            basic=self._basic,
            production=production,
            stats_today=stats_today,
            stats_month=stats_month,
            devices={device.sn: device for device in devices},
        )
        return self._with_live(data)

    async def _async_fetch_metadata(self, device_rows: list[dict[str, Any]]) -> None:
        """Fetch slow-changing data: station basic info and device information."""
        client = self.client
        basic, *infos = await asyncio.gather(
            client.get_station_basic_info(self.station_id),
            *(
                client.get_device_information(
                    self.station_id, row["sn"], row.get("deviceType", "")
                )
                for row in device_rows
                if row.get("sn")
            ),
        )
        self._basic = basic
        sns = [row["sn"] for row in device_rows if row.get("sn")]
        self._info = {
            sn: {code: factor.value for code, factor in flatten_factors(info).items()}
            for sn, info in zip(sns, infos, strict=True)
        }

    async def _async_fetch_device(self, row: dict[str, Any]) -> DeviceData:
        """Fetch telemetry, counters and related devices for one device."""
        client = self.client
        sn: str = row["sn"]
        device_type = str(row.get("deviceType", ""))
        calls = [
            client.get_device_telecounting(self.station_id, sn, device_type),
            client.get_device_telemetry(self.station_id, sn, device_type),
        ]
        if device_type == DEVICE_TYPE_INVERTER:
            calls.append(client.get_device_related(self.station_id, sn, device_type))
        telecounting, telemetry, *related = await asyncio.gather(*calls)
        return DeviceData(
            sn=sn,
            device_type=device_type,
            meta=row,
            # Telemetry wins over telecounting for codes both report.
            factors=flatten_factors(telecounting, telemetry),
            info=self._info.get(sn, {}),
            related=related[0] if related else [],
        )

    # ------------------------------------------------------------------
    # Live data
    # ------------------------------------------------------------------

    def _with_live(self, data: SemsPlusData) -> SemsPlusData:
        """Return data with the latest live values."""
        return replace(
            data,
            live=dict(self._live),
            flows=dict(self._flows),
            live_at=self._live_at,
            live_source=self._live_source,
        )

    def _publish_live(self) -> None:
        """Push live values to entities without resetting the poll schedule."""
        if self.data is None:
            return
        # async_set_updated_data would reschedule the poll on every live
        # message (every ~5 s), so the REST poll would never run.
        self.data = self._with_live(self.data)
        self.async_update_listeners()

    async def _async_merge_flow(self) -> None:
        """Fetch stations/flow and merge it as live data."""
        flow = await self.client.get_station_flow(self.station_id)
        values, flows = parse_flow_live(flow)
        if not values:
            return
        self._live.update(values)
        self._flows = flows
        self._live_at = dt_util.utcnow()
        self._live_source = "rest"

    @callback
    def async_handle_live(self, live: SemsLiveData) -> None:
        """Merge one MQTT live update."""
        self._live.update(live.values)
        self._flows = live.flows
        self._live_at = live.received_at
        self._live_source = "mqtt"
        self._publish_live()

    @callback
    def async_handle_mqtt_state(self, state: str) -> None:
        """Run the REST fallback while the live feed is down.

        Feed health (state, failures, last message) is read from the client by
        the live-feed entities; this only refreshes them.
        """
        _LOGGER.debug("SEMS+ live feed state: %s", state)
        if state == "connected":
            self._stop_fallback()
        elif self._fallback_unsub is None:
            self._fallback_unsub = async_track_time_interval(
                self.hass,
                self._async_fallback_refresh,
                self._fallback_interval,
                name=f"{DOMAIN} live fallback",
            )
        self._publish_live()

    async def _async_fallback_refresh(self, _now: datetime) -> None:
        """Poll stations/flow while the live feed is down."""
        try:
            await self._async_merge_flow()
        except SemsApiError as err:
            _LOGGER.debug(
                "SEMS+ live fallback for %s failed: %s",
                redact_for_log(self.station_id),
                err,
            )
            return
        self._publish_live()

    def _stop_fallback(self) -> None:
        if self._fallback_unsub is not None:
            self._fallback_unsub()
            self._fallback_unsub = None

    async def async_shutdown(self) -> None:
        """Cancel the fallback timer and shut down."""
        self._stop_fallback()
        await super().async_shutdown()
