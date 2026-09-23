"""Async client for the SEMS+ gateway API and its live MQTT feed."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlparse

import aiohttp
import aiomqtt
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util.ssl import get_default_context

from .auth import authenticated_headers, hash_password, login_headers
from .const import SemsRegion, redact_for_log, redact_value

_LOGGER = logging.getLogger(__name__)

# Dedicated logger passed to aiomqtt/paho-mqtt for the raw MQTT wire protocol.
_PROTOCOL_LOGGER = _LOGGER.getChild("protocol")

_StationsPageURLPart = "/sems-plant/api/portal/stations/page"
_StationFlowURLPart = "/sems-plant/api/stations/flow"
_StationStatisticsURLPart = "/sems-plant/api/stations/statistics"
_StationProductionURLPart = "/sems-plant/api/stations/production"
_StationBasicInfoURLPart = "/sems-plant/api/portal/stations/basic/info"
_StationDevicesURLPart = "/sems-plant/api/web/device/station/page"
_EquipmentURLPart = "/sems-plant/api/equipments/{sn}/{resource}"
_EnableSecondDataURLPart = "/sems-plant/api/second-data/enable"
_MqttConfigURLPart = "/sems-plant/api/second-data/config"
_LiveTopic = "/goodwe/second-data/station/{station_id}"

_RequestTimeout = 30  # seconds
_RateLimitRetryAfterSeconds = 300
_PageSize = 100

# Energy items (kWh) returned per day by stations/statistics.
StatisticsItems = (
    "proSystemTotalStats",
    "proConsumStats",
    "proGridStats",
    "proPurchaseStats",
    "proSelfConsumStats",
    "proCharStats",
    "proDischarStats",
)
# Items requested by the web UI for stations/production (energy + income).
_ProductionItems = (
    "proConsumStats",
    "proGridStats",
    "proPurchaseStats",
    "profitGridStats",
    "profitProStats",
    "proSystemTotalStats",
)

_SuccessCodes = {0, "0", "00000"}
_RateLimitCode = "GY0429"
_AuthErrorCodes = {"100002"}

_MqttKeepaliveSeconds = 60
# The web UI re-calls second-data/enable repeatedly while a station is open;
# without it the live feed goes quiet. The required interval is unverified.
_SecondDataKeepaliveSeconds = 60
_ReconnectDelay = 5
_MaxReconnectDelay = 60
_MqttFailedThreshold = 3

# Live message fields that are not numeric telemetry values.
_LiveNonNumericKeys = {"stationId", "time", "traceId", "flows"}

type LiveDataHandler = Callable[[SemsLiveData], None]
type MqttStatusHandler = Callable[[str], None]


class _RedactProtocolFilter(logging.Filter):
    """Redact station IDs and credentials from paho-mqtt's protocol log lines."""

    def filter(self, record: logging.LogRecord) -> bool:
        """Collapse args into msg and redact sensitive data."""
        if record.args:
            record.msg = record.getMessage()
            record.args = ()
        record.msg = redact_for_log(record.msg)
        return True


_PROTOCOL_LOGGER.addFilter(_RedactProtocolFilter())


class SemsApiError(HomeAssistantError):
    """Error to indicate a SEMS+ API request failed."""


class SemsAuthError(SemsApiError):
    """Error to indicate SEMS+ rejected the credentials."""


class OutOfRetries(SemsApiError):
    """Error to indicate too many token refresh attempts."""


class SemsRateLimitedError(SemsApiError):
    """Error to indicate the SEMS+ API requested retry with backoff."""

    def __init__(self, retry_after: int, message: str = "SEMS API rate limited"):
        """Initialize rate limit exception."""
        super().__init__(message)
        self.retry_after = retry_after


def _decimal_value(value: Any) -> Decimal | None:
    """Return a finite Decimal for numeric SEMS values."""
    if value in (None, "") or isinstance(value, bool):
        return None
    try:
        decimal_value = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return decimal_value if decimal_value.is_finite() else None


def decode_mqtt_payload(payload: bytes) -> Any:
    """Decode a SEMS MQTT payload, including any nested message JSON."""
    text = payload.decode("utf-8")
    try:
        decoded: Any = json.loads(text)
    except json.JSONDecodeError:
        return text

    if not isinstance(decoded, dict):
        return decoded

    nested_value = decoded.get("message", decoded.get("msg"))
    if not isinstance(nested_value, str):
        return decoded

    try:
        nested = json.loads(nested_value)
    except json.JSONDecodeError:
        return decoded

    decoded = dict(decoded)
    if "message" in decoded:
        decoded["message"] = nested
    else:
        decoded["msg"] = nested
    return decoded


@dataclass(frozen=True, slots=True)
class SemsLiveData:
    """One live station update from the SEMS+ MQTT feed.

    `values` holds every numeric field keyed by its API code (pSystem, pConsum,
    pGrid, pBat, pAc, pDc, qAc, fAc, pf, soc, ...) in the units SEMS sends
    (power in kW).
    """

    station_id: str
    time: str | None
    received_at: datetime
    values: dict[str, Decimal]
    flows: dict[str, list[str]]
    raw: dict[str, Any]

    @classmethod
    def from_payload(
        cls, payload: Any, received_at: datetime | None = None
    ) -> SemsLiveData | None:
        """Build live data from a decoded MQTT payload."""
        if not isinstance(payload, dict):
            return None

        message = payload.get("message", payload.get("msg", payload))
        if not isinstance(message, dict):
            return None

        station_id = message.get("stationId")
        if not isinstance(station_id, str) or not station_id:
            return None

        values: dict[str, Decimal] = {}
        for key, value in message.items():
            if key in _LiveNonNumericKeys:
                continue
            decimal_value = _decimal_value(value)
            if decimal_value is not None:
                values[key] = decimal_value

        flows: dict[str, list[str]] = {}
        raw_flows = message.get("flows")
        if isinstance(raw_flows, dict):
            for source, targets in raw_flows.items():
                if isinstance(source, str) and isinstance(targets, list):
                    flows[source] = [t for t in targets if isinstance(t, str)]

        time_value = message.get("time")
        return cls(
            station_id=station_id,
            time=time_value if isinstance(time_value, str) else None,
            received_at=received_at or datetime.now(UTC),
            values=values,
            flows=flows,
            raw=message,
        )


class SemsPlusClient:
    """Interface to the SEMS+ gateway API and live MQTT feed."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        username: str,
        password: str,
        region: SemsRegion,
    ) -> None:
        """Initialize the client."""
        self._session = session
        self._username = username
        self._password = password
        self._region = region
        self._token: dict[str, Any] | None = None
        self._login_lock = asyncio.Lock()

        self._mqtt_task: asyncio.Task[None] | None = None
        self._mqtt_stop = asyncio.Event()
        self._mqtt_status_handler: MqttStatusHandler | None = None
        self._mqtt_state = "disconnected"
        self._mqtt_failures = 0
        self._mqtt_reported: tuple[str, int] = ("disconnected", 0)
        self._mqtt_last_message_at: datetime | None = None

    @property
    def region(self) -> SemsRegion:
        """Return the region endpoints this client uses."""
        return self._region

    # ------------------------------------------------------------------
    # Request core
    # ------------------------------------------------------------------

    async def _make_http_request(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        *,
        params: dict[str, Any] | None = None,
        json_data: dict[str, Any] | None = None,
        operation_name: str,
        validate_code: bool = True,
    ) -> dict[str, Any] | None:
        """Make an HTTP request and return the JSON body.

        Returns None when SEMS reports an expired or invalid session so the
        caller can refresh the token and retry.
        """
        _LOGGER.debug("SEMS - Making %s to %s", operation_name, url)
        try:
            async with self._session.request(
                method,
                url,
                headers=headers,
                params=params,
                json=json_data,
                timeout=aiohttp.ClientTimeout(total=_RequestTimeout),
            ) as response:
                if response.status == 401:
                    _LOGGER.debug(
                        "%s - HTTP 401, will retry with fresh token", operation_name
                    )
                    return None
                response.raise_for_status()
                json_response = await response.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError, ValueError) as err:
            _LOGGER.error("Unable to complete %s: %s", operation_name, err)
            raise SemsApiError(f"{operation_name} failed: {err}") from err

        if not isinstance(json_response, dict):
            raise SemsApiError(f"{operation_name} returned a non-object response")

        response_code = json_response.get("code")
        _LOGGER.debug(
            "SEMS - %s response: %s",
            operation_name,
            redact_for_log(json_response),
        )

        if str(response_code) == _RateLimitCode:
            raise SemsRateLimitedError(
                retry_after=_RateLimitRetryAfterSeconds,
                message=f"{operation_name} returned rate-limit code {_RateLimitCode}",
            )

        if not validate_code or response_code in _SuccessCodes:
            return json_response

        error_msg = str(
            json_response.get("msg")
            or json_response.get("description")
            or "Unknown error"
        )
        if (
            str(response_code) in _AuthErrorCodes
            or "authorization" in error_msg.lower()
        ):
            _LOGGER.debug(
                "%s - Authorization expired (code: %s): %s. Will retry with fresh token.",
                operation_name,
                response_code,
                error_msg,
            )
            return None

        _LOGGER.error(
            "%s failed with code: %s, message: %s",
            operation_name,
            response_code,
            error_msg,
        )
        raise SemsApiError(
            f"{operation_name} failed with code {response_code}: {error_msg}"
        )

    async def _login(self) -> dict[str, Any]:
        """Authenticate and return the token payload used for gateway calls."""
        operation_name = "SEMS+ login"
        _LOGGER.debug(
            "SEMS+ authentication attempt: account=%s endpoint=%s",
            redact_value(self._username),
            self._region.login_url,
        )
        json_response = await self._make_http_request(
            "POST",
            self._region.login_url,
            login_headers(),
            json_data={
                "account": self._username,
                "pwd": hash_password(self._password),
                "agreement": 1,
                "isLocal": False,
                "isChinese": False,
            },
            operation_name=operation_name,
            validate_code=False,
        )
        if json_response is None:
            raise SemsAuthError("SEMS+ login was rejected (HTTP 401)")

        code = json_response.get("code")
        token_data = json_response.get("data")
        if (
            code not in _SuccessCodes
            or not isinstance(token_data, dict)
            or not token_data.get("token")
        ):
            raise SemsAuthError(
                f"SEMS+ login rejected: code={code} "
                f"msg={json_response.get('msg') or json_response.get('description')}"
            )

        token = dict(token_data)
        _LOGGER.debug("SEMS - API token received: %s", redact_for_log(token))
        return token

    async def _get_token(self, renew: bool = False) -> dict[str, Any]:
        """Return the current token, logging in once if missing or renewing."""
        stale_token = self._token
        async with self._login_lock:
            # Another caller may have already refreshed while we waited.
            if self._token is None or (renew and self._token is stale_token):
                self._token = await self._login()
            return self._token

    async def _make_api_call(
        self,
        url_part: str,
        *,
        method: str = "GET",
        params: dict[str, Any] | None = None,
        json_data: dict[str, Any] | None = None,
        operation_name: str = "API call",
        renew_token: bool = False,
        max_token_retries: int = 2,
    ) -> Any:
        """Make an authenticated call and return its `data` payload."""
        url = self._region.gateway_api_url + url_part
        for attempt in range(max_token_retries):
            token = await self._get_token(renew=renew_token or attempt > 0)
            json_response = await self._make_http_request(
                method,
                url,
                authenticated_headers(token),
                params=params,
                json_data=json_data,
                operation_name=operation_name,
            )
            if json_response is not None:
                return json_response.get("data")

            _LOGGER.info(
                "SEMS - %s authorization expired. Refreshing token and retrying "
                "(%s attempts remaining)...",
                operation_name,
                max_token_retries - attempt - 1,
            )

        raise OutOfRetries(
            f"SEMS - {operation_name} failed: maximum token refresh attempts exceeded"
        )

    async def _get_paged(
        self, url_part: str, body: dict[str, Any], operation_name: str
    ) -> list[dict[str, Any]]:
        """Collect every row from a paginated `dataList` endpoint."""
        rows: list[dict[str, Any]] = []
        page = 1
        while True:
            result = await self._make_api_call(
                url_part,
                method="POST",
                json_data={**body, "current": page, "size": _PageSize},
                operation_name=f"{operation_name} (page {page})",
            )
            if not isinstance(result, dict):
                return rows

            data_list = result.get("dataList")
            if not isinstance(data_list, list) or not data_list:
                return rows

            rows.extend(row for row in data_list if isinstance(row, dict))

            total = result.get("total")
            if not isinstance(total, int) or len(rows) >= total:
                return rows
            page += 1

    async def _get_equipment(
        self,
        sn: str,
        resource: str,
        params: dict[str, Any] | None,
        operation_name: str,
    ) -> Any:
        """Call one of the per-device `equipments/{sn}/...` endpoints."""
        return await self._make_api_call(
            _EquipmentURLPart.format(sn=sn, resource=resource),
            params=params,
            operation_name=operation_name,
        )

    # ------------------------------------------------------------------
    # Account
    # ------------------------------------------------------------------

    async def login(self) -> dict[str, Any]:
        """Log in and return the token payload."""
        async with self._login_lock:
            self._token = await self._login()
            return self._token

    async def test_authentication(self) -> bool:
        """Return whether the credentials are accepted."""
        try:
            await self.login()
        except SemsApiError as err:
            _LOGGER.error("SEMS+ authentication failed: %s", err)
            return False
        return True

    async def get_version(self) -> dict[str, Any]:
        """Return the SEMS+ web build version (unauthenticated)."""
        json_response = await self._make_http_request(
            "GET",
            f"{self._region.web_origin}/version.json",
            {"Accept": "application/json"},
            params={"t": round(time.time() * 1000)},
            operation_name="getVersion API call",
        )
        return json_response or {}

    # ------------------------------------------------------------------
    # Stations
    # ------------------------------------------------------------------

    async def get_stations(self) -> list[dict[str, Any]]:
        """Return every station available to the account."""
        return await self._get_paged(_StationsPageURLPart, {}, "getStations API call")

    async def get_station_flow(self, station_id: str) -> dict[str, Any]:
        """Return the station's current power flow (values in kW)."""
        result = await self._make_api_call(
            _StationFlowURLPart,
            params={"stationId": station_id},
            operation_name="getStationFlow API call",
        )
        return result if isinstance(result, dict) else {}

    async def get_station_statistics(
        self,
        station_id: str,
        start: date,
        end: date,
        items: Sequence[str] = StatisticsItems,
    ) -> dict[str, Any]:
        """Return daily energy statistics (kWh) for a date range.

        `dataList[].statisticsList` holds one `{date, val}` entry per day; the
        top-level totals (proGrid, proPurchase, rates...) cover the whole range.
        """
        result = await self._make_api_call(
            _StationStatisticsURLPart,
            method="POST",
            json_data={
                "stationId": station_id,
                "isReport": False,
                "items": list(items),
                "dimension": "day",
                "startTime": f"{start.isoformat()} 00:00:00",
                "endTime": f"{end.isoformat()} 23:59:59",
            },
            operation_name="getStationStatistics API call",
        )
        return result if isinstance(result, dict) else {}

    async def get_station_production(
        self, station_id: str, day: date
    ) -> dict[str, Any]:
        """Return one day's production energy and income (with `currency`)."""
        result = await self._make_api_call(
            _StationProductionURLPart,
            method="POST",
            json_data={
                "stationId": station_id,
                "items": list(_ProductionItems),
                "dimension": "day",
                "isReport": False,
                "startTime": f"{day.isoformat()} 00:00:00",
                "endTime": f"{day.isoformat()} 23:59:59",
            },
            operation_name="getStationProduction API call",
        )
        return result if isinstance(result, dict) else {}

    async def get_station_basic_info(self, station_id: str) -> dict[str, Any]:
        """Return station metadata (battery/PV capacity, grid status, zone...)."""
        result = await self._make_api_call(
            _StationBasicInfoURLPart,
            method="POST",
            params={"stationId": station_id},
            json_data={},
            operation_name="getStationBasicInfo API call",
        )
        return result if isinstance(result, dict) else {}

    async def get_station_devices(self, station_id: str) -> list[dict[str, Any]]:
        """Return every device in a station with its model and type."""
        return await self._get_paged(
            _StationDevicesURLPart,
            {"stationId": station_id},
            "getStationDevices API call",
        )

    # ------------------------------------------------------------------
    # Devices
    # ------------------------------------------------------------------

    async def get_device_information(
        self, station_id: str, sn: str, device_type: str
    ) -> list[dict[str, Any]]:
        """Return the device's information factors (name, model, firmware...)."""
        result = await self._get_equipment(
            sn,
            "information",
            {"deviceType": device_type, "pwId": station_id},
            "getDeviceInformation API call",
        )
        return result if isinstance(result, list) else []

    async def get_device_related(
        self, station_id: str, sn: str, device_type: str
    ) -> list[dict[str, Any]]:
        """Return devices related to this one (e.g. battery systems, inverter)."""
        result = await self._get_equipment(
            sn,
            "relatedDevices",
            {"sn": sn, "deviceType": device_type, "pwId": station_id},
            "getDeviceRelated API call",
        )
        return result if isinstance(result, list) else []

    async def get_device_telecounting(
        self, station_id: str, sn: str, device_type: str
    ) -> list[dict[str, Any]]:
        """Return the device's energy counters grouped by period."""
        result = await self._get_equipment(
            sn,
            "telecounting",
            {"deviceType": device_type, "pwId": station_id},
            "getDeviceTelecounting API call",
        )
        return result if isinstance(result, list) else []

    async def get_device_telemetry(
        self, station_id: str, sn: str, device_type: str
    ) -> list[dict[str, Any]]:
        """Return the device's telemetry factors grouped by parameter section."""
        result = await self._get_equipment(
            sn,
            "telemetry",
            {"deviceType": device_type, "pwId": station_id},
            "getDeviceTelemetry API call",
        )
        return result if isinstance(result, list) else []

    # ------------------------------------------------------------------
    # Live data (MQTT)
    # ------------------------------------------------------------------

    async def enable_second_data(self, station_id: str) -> bool:
        """Enable second-data (MQTT live updates) for a station."""
        result = await self._make_api_call(
            _EnableSecondDataURLPart,
            params={"stationId": station_id},
            operation_name="enableSecondData API call",
        )
        return result is True

    async def get_mqtt_config(self) -> dict[str, Any]:
        """Return the short-lived MQTT credentials (clientId/userName/password)."""
        result = await self._make_api_call(
            _MqttConfigURLPart,
            operation_name="getMqttConfig API call",
        )
        config = result if isinstance(result, dict) else {}
        _LOGGER.debug("SEMS MQTT configuration: %s", redact_for_log(config))
        return config

    @property
    def mqtt_state(self) -> str:
        """Return the live feed state: disconnected, connecting, connected, failed."""
        return self._mqtt_state

    @property
    def mqtt_connection_failures(self) -> int:
        """Return the number of consecutive live feed connection failures."""
        return self._mqtt_failures

    @property
    def mqtt_last_message_at(self) -> datetime | None:
        """Return when the last live update for the station was received."""
        return self._mqtt_last_message_at

    def start_mqtt(
        self,
        station_id: str,
        on_update: LiveDataHandler,
        on_status: MqttStatusHandler | None = None,
    ) -> None:
        """Start the live feed for a station in the background.

        `on_update` receives a SemsLiveData for every station message and
        `on_status` receives each connection state change. Does nothing if the
        feed is already running.
        """
        if self._mqtt_task is not None and not self._mqtt_task.done():
            return

        self._mqtt_stop = asyncio.Event()
        self._mqtt_status_handler = on_status
        self._mqtt_failures = 0
        self._mqtt_task = asyncio.create_task(
            self._mqtt_loop(station_id, on_update),
            name=f"SEMS+ live feed {redact_value(station_id)}",
        )

    async def stop_mqtt(self) -> None:
        """Stop the live feed and wait for it to shut down."""
        _LOGGER.debug("Requesting SEMS MQTT live feed to stop...")
        self._mqtt_stop.set()
        task, self._mqtt_task = self._mqtt_task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self._set_mqtt_state("disconnected")

    def _set_mqtt_state(self, state: str) -> None:
        """Record the live feed state and notify when it or the failure count changed."""
        reported = (state, self._mqtt_failures)
        if reported == self._mqtt_reported:
            return
        self._mqtt_state = state
        self._mqtt_reported = reported
        if self._mqtt_status_handler is not None:
            self._mqtt_status_handler(state)

    def _mqtt_client_args(self, config: dict[str, Any]) -> dict[str, Any]:
        """Build aiomqtt.Client arguments from the MQTT config response."""
        client_id = config.get("clientId")
        username = config.get("userName")
        password = config.get("password")
        if not all(
            isinstance(value, str) and value
            for value in (client_id, username, password)
        ):
            raise ValueError("SEMS MQTT configuration is missing required fields")

        parsed_url = urlparse(self._region.mqtt_broker_url)
        if parsed_url.scheme not in {"ws", "wss"} or not parsed_url.hostname:
            raise ValueError("SEMS MQTT broker URL must use ws:// or wss://")

        use_tls = parsed_url.scheme == "wss"
        return {
            "hostname": parsed_url.hostname,
            "port": parsed_url.port or (443 if use_tls else 80),
            "websocket_path": parsed_url.path or "/",
            "tls_context": get_default_context() if use_tls else None,
            "identifier": client_id,
            "username": username,
            "password": password,
        }

    def _get_backoff_delay(self, failures: int) -> int:
        """Return an exponential backoff delay capped at the maximum retry window."""
        if failures <= 0:
            return _ReconnectDelay
        delay = _ReconnectDelay * (2 ** min(failures, 4))
        return min(delay, _MaxReconnectDelay)

    async def _mqtt_loop(self, station_id: str, on_update: LiveDataHandler) -> None:
        """Connect and listen until stopped, reconnecting with backoff."""
        topic = _LiveTopic.format(station_id=station_id)
        while not self._mqtt_stop.is_set():
            client_args: dict[str, Any] | None = None
            try:
                self._set_mqtt_state("connecting")
                if not await self.enable_second_data(station_id):
                    raise SemsApiError("SEMS second-data enable call failed")
                client_args = self._mqtt_client_args(await self.get_mqtt_config())

                _LOGGER.debug(
                    "Opening SEMS MQTT websocket to %s:%d%s",
                    client_args["hostname"],
                    client_args["port"],
                    client_args["websocket_path"],
                )
                async with aiomqtt.Client(
                    **client_args,
                    protocol=aiomqtt.ProtocolVersion.V311,
                    transport="websockets",
                    keepalive=_MqttKeepaliveSeconds,
                    logger=_PROTOCOL_LOGGER,
                ) as client:
                    await client.subscribe(topic, qos=0)
                    self._mqtt_failures = 0
                    self._set_mqtt_state("connected")
                    _LOGGER.info(
                        "SEMS MQTT connected and subscribed to %s",
                        redact_for_log(topic),
                    )
                    keepalive_task = asyncio.create_task(
                        self._second_data_keepalive(station_id)
                    )
                    try:
                        async for message in client.messages:
                            payload = message.payload
                            if isinstance(payload, str):
                                payload = payload.encode()
                            if isinstance(payload, (bytes, bytearray)):
                                self._handle_mqtt_message(
                                    station_id, bytes(payload), on_update
                                )
                    finally:
                        keepalive_task.cancel()
                        await asyncio.gather(keepalive_task, return_exceptions=True)
            except asyncio.CancelledError:
                raise
            except (aiomqtt.MqttError, SemsApiError, ValueError) as err:
                self._mqtt_failures += 1
                self._set_mqtt_state(
                    "failed"
                    if self._mqtt_failures >= _MqttFailedThreshold
                    else "connecting"
                )
                _LOGGER.debug(
                    "SEMS MQTT connection attempt %d failed (broker=%s): %s",
                    self._mqtt_failures,
                    client_args["hostname"] if client_args else "unknown",
                    err,
                )
                if self._mqtt_failures >= _MqttFailedThreshold:
                    _LOGGER.warning(
                        "SEMS MQTT has failed %d consecutive connection attempts; "
                        "retrying in background",
                        self._mqtt_failures,
                    )

            if self._mqtt_stop.is_set():
                break

            delay = self._get_backoff_delay(self._mqtt_failures)
            _LOGGER.debug(
                "Reconnecting to SEMS MQTT in %d seconds (failure count: %d)...",
                delay,
                self._mqtt_failures,
            )
            try:
                await asyncio.wait_for(self._mqtt_stop.wait(), timeout=delay)
            except TimeoutError:
                pass

        self._set_mqtt_state("disconnected")

    async def _second_data_keepalive(self, station_id: str) -> None:
        """Keep re-enabling second-data so SEMS keeps publishing live updates."""
        while True:
            await asyncio.sleep(_SecondDataKeepaliveSeconds)
            try:
                if not await self.enable_second_data(station_id):
                    _LOGGER.warning("SEMS second-data keepalive returned false")
            except SemsApiError as err:
                _LOGGER.warning("SEMS second-data keepalive failed: %s", err)

    def _handle_mqtt_message(
        self, station_id: str, payload: bytes, on_update: LiveDataHandler
    ) -> None:
        """Decode one MQTT message and pass station updates to the handler."""
        try:
            decoded = decode_mqtt_payload(payload)
        except UnicodeDecodeError:
            _LOGGER.debug("SEMS live message binary_length=%s", len(payload))
            return

        _LOGGER.debug("SEMS live message payload=%s", redact_for_log(decoded))

        live_data = SemsLiveData.from_payload(decoded)
        if live_data is None or live_data.station_id != station_id:
            return

        self._mqtt_last_message_at = live_data.received_at
        try:
            on_update(live_data)
        except Exception:
            _LOGGER.exception("SEMS live data handler raised")
