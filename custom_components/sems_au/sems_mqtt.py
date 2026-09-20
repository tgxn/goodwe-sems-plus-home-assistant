"""Read-only MQTT-over-WebSocket listener for SEMS live data."""

from __future__ import annotations

import asyncio
import json
import logging
import ssl
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlparse

import aiomqtt
from homeassistant.core import HomeAssistant

from .const import (
    DEFAULT_SEMS_REGION,
    SEMS_REGIONS,
    redact_for_log,
    redact_text,
)
from .sems_api import SemsApi

_LOGGER = logging.getLogger(__name__)

# Dedicated logger passed to aiomqtt/paho-mqtt so the raw MQTT wire protocol
# (CONNECT/CONNACK, SUBSCRIBE/SUBACK, PUBLISH, PINGREQ/PINGRESP) is visible at
# DEBUG level. paho-mqtt's enable_logger() only wires up whatever logger
# object it is given, so this must be passed explicitly to aiomqtt.Client.
_PROTOCOL_LOGGER = _LOGGER.getChild("protocol")
_PROTOCOL_LOGGER.setLevel(logging.DEBUG)

_RECONNECT_DELAY = 5
_MAX_RECONNECT_DELAY = 60


class _RedactStationIdFilter(logging.Filter):
    """Redact station IDs embedded in paho-mqtt's raw protocol log lines."""

    def filter(self, record: logging.LogRecord) -> bool:
        """Collapse args into msg and redact any embedded station ID."""
        if record.args:
            record.msg = record.getMessage()
            record.args = ()
        record.msg = redact_text(str(record.msg))
        return True


_PROTOCOL_LOGGER.addFilter(_RedactStationIdFilter())

type MqttMessageHandler = Callable[[dict[str, Any]], None]


@dataclass(frozen=True, slots=True)
class SemsMqttConfig:
    """Validated SEMS MQTT connection configuration."""

    hostname: str
    port: int
    websocket_path: str
    websocket_origin: str
    use_tls: bool
    client_id: str
    username: str
    password: str

    @classmethod
    def from_api(
        cls,
        data: dict[str, Any],
        region: str = DEFAULT_SEMS_REGION,
    ) -> SemsMqttConfig:
        """Build connection configuration from the SEMS API response."""
        region_config = SEMS_REGIONS.get(region)
        if region_config is None:
            raise ValueError(f"Unsupported SEMS region: {region}")

        client_id = data.get("clientId")
        username = data.get("userName")
        password = data.get("password")
        if not all(
            isinstance(value, str) and value
            for value in (client_id, username, password)
        ):
            raise ValueError("SEMS MQTT configuration is missing required fields")

        assert isinstance(client_id, str)
        assert isinstance(username, str)
        assert isinstance(password, str)

        parsed_url = urlparse(region_config.mqtt_broker_url)
        if parsed_url.scheme not in {"ws", "wss"} or not parsed_url.hostname:
            raise ValueError("SEMS MQTT broker URL must use ws:// or wss://")

        use_tls = parsed_url.scheme == "wss"
        return cls(
            hostname=parsed_url.hostname,
            port=parsed_url.port or (443 if use_tls else 80),
            websocket_path=parsed_url.path or "/",
            websocket_origin=region_config.web_origin,
            use_tls=use_tls,
            client_id=client_id,
            username=username,
            password=password,
        )


def decode_mqtt_payload(payload: bytes) -> Any:
    """Decode a SEMS MQTT payload, including its nested message JSON."""
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


def _decimal_value(value: Any) -> Decimal | None:
    """Return a Decimal for numeric MQTT values."""

    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _kw_to_watts(value: Any) -> Decimal | None:
    """Convert a kW MQTT value to W."""

    decimal_value = _decimal_value(value)
    if decimal_value is None:
        return None
    return decimal_value * Decimal("1000")


def normalize_mqtt_powerflow_payload(payload: Any) -> dict[str, Any] | None:
    """Normalize one SEMS MQTT station payload to coordinator powerflow keys."""

    if not isinstance(payload, dict):
        return None

    message = payload.get("message", payload.get("msg", payload))
    if not isinstance(message, dict):
        return None

    normalized: dict[str, Any] = {"source": "mqtt"}
    mappings = {
        "pSystem": ("pv", _kw_to_watts),
        "pConsum": ("load", _kw_to_watts),
        "pGrid": ("grid", _kw_to_watts),
        "pBat": ("battery", _kw_to_watts),
        "pAc": ("ac_power", _kw_to_watts),
        "pDc": ("dc_power", _kw_to_watts),
        "qAc": ("reactive_power", _kw_to_watts),
        "fAc": ("grid_frequency", _decimal_value),
        "pf": ("power_factor", _decimal_value),
        "soc": ("soc", _decimal_value),
    }
    for source_key, (target_key, converter) in mappings.items():
        converted = converter(message.get(source_key))
        if converted is not None:
            normalized[target_key] = converted

    if "pv" in normalized:
        normalized["system_power"] = normalized["pv"]

    for source_key, target_key in (
        ("stationId", "station_id"),
        ("time", "last_live_update"),
        ("traceId", "trace_id"),
    ):
        value = message.get(source_key)
        if isinstance(value, str) and value:
            normalized[target_key] = value

    flows = message.get("flows")
    if isinstance(flows, dict):
        normalized["flows"] = flows

    return normalized if len(normalized) > 1 else None


class SemsMqttListener:
    """Maintain a SEMS MQTT connection and log live station messages."""

    def __init__(
        self,
        hass: HomeAssistant,
        api: SemsApi,
        station_id: str,
        region: str = DEFAULT_SEMS_REGION,
        message_handler: MqttMessageHandler | None = None,
    ) -> None:
        """Initialize the listener."""
        self._hass = hass
        self._api = api
        self._topic = f"/goodwe/second-data/station/{station_id}"
        self._region = region
        self._message_handler = message_handler
        self._stop_event = asyncio.Event()
        self._connection_failures = 0
        self._is_connected = False
        self._last_update_time: str | None = None
        self._connection_state = (
            "disconnected"  # disconnected, connecting, connected, failed
        )

    def _get_backoff_delay(self, failures: int) -> int:
        """Return an exponential backoff delay capped at the maximum retry window."""
        if failures <= 0:
            return _RECONNECT_DELAY
        delay = _RECONNECT_DELAY * (2 ** min(failures, 4))
        return min(delay, _MAX_RECONNECT_DELAY)

    @property
    def is_connected(self) -> bool:
        """Return whether the MQTT connection is currently active."""
        return self._is_connected

    @property
    def connection_state(self) -> str:
        """Return the current connection state."""
        return self._connection_state

    @property
    def connection_failures(self) -> int:
        """Return the number of connection failures."""
        return self._connection_failures

    @property
    def last_update_time(self) -> str | None:
        """Return the timestamp of the last successful MQTT message."""
        return self._last_update_time

    async def async_run(self) -> None:
        """Connect and listen until stopped or cancelled."""
        while not self._stop_event.is_set():
            try:
                self._connection_state = "connecting"
                _LOGGER.debug("Attempting to connect to SEMS MQTT broker...")

                config_data = await self._hass.async_add_executor_job(
                    self._api.getMqttConfig
                )
                config = SemsMqttConfig.from_api(config_data, self._region)
                if config.use_tls:
                    tls_context = await self._hass.async_add_executor_job(
                        ssl.create_default_context
                    )
                else:
                    tls_context = None

                # Warm up session by making a REST call first
                # This ensures the MQTT broker recognizes us as an authenticated session
                try:
                    await self._hass.async_add_executor_job(
                        self._api.getData,
                        self._topic.split("/")[-1],  # Extract station_id from topic
                    )
                    _LOGGER.debug(
                        "Session warm-up complete, MQTT broker should recognize us"
                    )
                except Exception as warmup_err:
                    _LOGGER.debug(
                        "Session warm-up call failed (continuing): %s", warmup_err
                    )

                # Delay to let session settle and avoid rate limiting
                await asyncio.sleep(2)

                _LOGGER.debug(
                    "SEMS MQTT stage 1/3: opening WebSocket + MQTT CONNECT to %s:%d%s",
                    config.hostname,
                    config.port,
                    config.websocket_path,
                )

                async with aiomqtt.Client(
                    hostname=config.hostname,
                    port=config.port,
                    username=config.username,
                    password=config.password,
                    identifier=config.client_id,
                    protocol=aiomqtt.ProtocolVersion.V311,
                    transport="websockets",
                    tls_context=tls_context,
                    websocket_path=config.websocket_path,
                    logger=_PROTOCOL_LOGGER,
                ) as client:
                    self._connection_failures = 0
                    self._is_connected = True
                    self._connection_state = "connected"
                    _LOGGER.debug(
                        "SEMS MQTT stage 2/3: WebSocket handshake and MQTT "
                        "CONNECT/CONNACK complete"
                    )
                    await client.subscribe(self._topic, qos=0)
                    _LOGGER.info(
                        "✓ SEMS MQTT stage 3/3: SUBSCRIBE/SUBACK complete, connected to "
                        "SEMS live data and subscribed to %s",
                        redact_for_log(self._topic),
                    )
                    async for message in client.messages:
                        if self._stop_event.is_set():
                            break
                        self._handle_message(str(message.topic), bytes(message.payload))
            except asyncio.CancelledError:
                raise
            except (aiomqtt.MqttError, UnicodeDecodeError, ValueError) as err:
                self._connection_failures += 1
                self._is_connected = False
                self._connection_state = (
                    "failed" if self._connection_failures >= 3 else "connecting"
                )
                error_type = type(err).__name__
                error_details = str(err)
                broker_host = config.hostname if "config" in locals() else "unknown"
                broker_port = config.port if "config" in locals() else 0
                broker_path = (
                    config.websocket_path if "config" in locals() else "unknown"
                )
                _LOGGER.debug(
                    "SEMS MQTT connection attempt %d failed (state=%s, error_type=%s, broker=%s:%d%s): %s",
                    self._connection_failures,
                    self._connection_state,
                    error_type,
                    broker_host,
                    broker_port,
                    broker_path,
                    error_details,
                )
                # Only log WARNING if we're in failed state (after 3+ attempts)
                if self._connection_failures >= 3:
                    _LOGGER.warning(
                        "✗ SEMS MQTT connection failed after %d attempts (state=%s, error_type=%s, broker=%s:%d): Check network connectivity, MQTT broker status, and SEMS credentials. Will continue retrying in background.",
                        self._connection_failures,
                        self._connection_state,
                        error_type,
                        broker_host,
                        broker_port,
                    )

            if self._stop_event.is_set():
                self._connection_state = "disconnected"
                self._is_connected = False
                break

            delay = self._get_backoff_delay(self._connection_failures)
            _LOGGER.debug(
                "Reconnecting to SEMS MQTT in %d seconds (failure count: %d)...",
                delay,
                self._connection_failures,
            )
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=delay)
            except TimeoutError:
                pass

    def stop(self) -> None:
        """Request that the listener stop."""
        self._is_connected = False
        self._connection_state = "disconnected"
        _LOGGER.debug("Requesting SEMS MQTT listener to stop...")
        self._stop_event.set()

    def _handle_message(self, topic: str, payload: bytes) -> None:
        """Decode and safely log one MQTT message."""
        try:
            decoded = decode_mqtt_payload(payload)
        except UnicodeDecodeError:
            _LOGGER.debug(
                "SEMS live data message topic=%s binary_length=%s",
                redact_for_log(topic),
                len(payload),
            )
            return

        _LOGGER.debug(
            "SEMS live data message topic=%s payload=%s",
            redact_for_log(topic),
            redact_for_log(decoded),
        )

        normalized = normalize_mqtt_powerflow_payload(decoded)
        if normalized is not None and self._message_handler is not None:
            # Update the last update time from the message
            if isinstance(normalized, dict):
                self._last_update_time = normalized.get("last_live_update")
            self._message_handler(normalized)
