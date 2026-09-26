"""Constants for the SEMS+ integration."""

from __future__ import annotations

import dataclasses
import re
from typing import Any

DOMAIN = "sems_plus"

PLATFORMS = ["binary_sensor", "sensor"]

CONF_STATION_ID = "station_id"
CONF_REGION = "region"
CONF_POLL_INTERVAL = "poll_interval"
CONF_FALLBACK_INTERVAL = "fallback_interval"
CONF_METADATA_INTERVAL = "metadata_interval"

DEFAULT_POLL_INTERVAL = 300  # seconds; REST poll for totals and device values
DEFAULT_FALLBACK_INTERVAL = 120  # seconds; live power polling while MQTT is down
DEFAULT_METADATA_INTERVAL = 3600  # seconds; firmware, capacities, income

MIN_POLL_INTERVAL = 60
MIN_FALLBACK_INTERVAL = 30
MIN_METADATA_INTERVAL = 600


@dataclasses.dataclass(frozen=True, slots=True)
class SemsRegion:
    """SEMS+ endpoints for a supported region."""

    login_url: str
    gateway_api_url: str
    mqtt_broker_url: str
    web_origin: str


SEMS_REGIONS = {
    "AU": SemsRegion(
        login_url="https://au-semsplus.goodwe.com/web/sems/sems-user/api/v1/auth/cross-login",
        gateway_api_url="https://au-gateway.semsportal.com/web/sems",
        mqtt_broker_url="wss://netty-wss-au.iot.goodwe-power.com:8885/mqtt",
        web_origin="https://au-semsplus.goodwe.com",
    ),
}
DEFAULT_SEMS_REGION = "AU"


def redact_value(value: str) -> str:
    """Return a partial redaction of a sensitive value for logging.

    Shows enough of the value to remain unique/recognizable while hiding
    the sensitive information.
    """
    if not value:
        return "<redacted>"

    # For email addresses, show domain but redact local part
    if "@" in value:
        parts = value.rsplit("@", 1)
        return f"<***@{parts[1]}>"

    # For UUIDs (8-4-4-4-12 pattern with hyphens)
    if value.count("-") == 4 and len(value) == 36:
        return f"<{value[:4]}...{value[-4:]}>"

    # For longer strings (SNs, IDs), show first and last few chars
    if len(value) > 8:
        return f"<{value[:3]}...{value[-3:]}>"

    # For short strings, just show pattern
    return f"<{value[0]}{'*' * (len(value) - 1)}>"


_SENSITIVE_LOG_KEYS = {
    "account",
    "pwd",
    "password",
    "username",
    "clientid",
    "token",
    "uid",
    "uuid",
    "sn",
    "snlist",
    "snquery",
    "stationid",
    "pwid",
    "googleaddress",
}

_EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_UUID_PATTERN = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
_SERIAL_PATTERN = re.compile(r"^(?=.*[A-Za-z])(?=.*\d)[A-Za-z0-9]{12,26}$")
_EMBEDDED_UUID_PATTERN = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)


def _matches_sensitive_pattern(value: str) -> bool:
    """Return whether a string looks sensitive by format."""
    return bool(
        _EMAIL_PATTERN.fullmatch(value)
        or _UUID_PATTERN.fullmatch(value)
        or _SERIAL_PATTERN.fullmatch(value)
        or _EMBEDDED_UUID_PATTERN.search(value)
    )


def _is_sensitive_label(key: Any) -> bool:
    """Return whether a dictionary key implies its value is sensitive."""
    return isinstance(key, str) and key.lower() in _SENSITIVE_LOG_KEYS


def _redact_sensitive_value(value: Any) -> Any:
    """Redact a value associated with a sensitive key."""
    if isinstance(value, str):
        return redact_value(value)

    if isinstance(value, dict):
        return {
            key: _redact_sensitive_value(sub_value) for key, sub_value in value.items()
        }

    if isinstance(value, list):
        return [_redact_sensitive_value(item) for item in value]

    # For non-string, non-container types (numbers, booleans, None, etc.), keep as-is
    return value


def redact_for_log(value: Any) -> Any:
    """Return a redacted structure suitable for debug logging."""
    if isinstance(value, str):
        return redact_value(value) if _matches_sensitive_pattern(value) else value

    if isinstance(value, dict):
        sanitized: dict[Any, Any] = {}
        for key, sub_value in value.items():
            if isinstance(key, str) and _matches_sensitive_pattern(key):
                sanitized[redact_value(key)] = _redact_sensitive_value(sub_value)
            elif _is_sensitive_label(key):
                sanitized[key] = _redact_sensitive_value(sub_value)
            else:
                sanitized[key] = redact_for_log(sub_value)
        return sanitized

    if isinstance(value, list):
        return [redact_for_log(item) for item in value]

    # Handle dataclass instances by converting to dict and recursing
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return redact_for_log(dataclasses.asdict(value))

    return value
