from __future__ import annotations

import base64
import hashlib
import json
import logging
import time
from dataclasses import dataclass
from typing import Any

import requests
from homeassistant import exceptions
from homeassistant.core import HomeAssistant

from .const import DEFAULT_SEMS_REGION, SEMS_REGIONS, redact_for_log, redact_value

_LOGGER = logging.getLogger(__name__)

_PowerStationURLPart = "/v3/PowerStation/GetMonitorDetailByPowerstationId"
_EnableSecondDataURLPart = "/sems-plant/api/second-data/enable"
_MqttConfigURLPart = "/sems-plant/api/second-data/config"
_StationsPageURLPart = "/sems-plant/api/portal/stations/page"
_RequestTimeout = 30  # seconds
_RateLimitRetryAfterSeconds = 300
_StationsPageSize = 100

_SuccessCodes = {0, "0", "00000"}
_RateLimitCode = "GY0429"

_NewSEMSPlusWebLoginHeaders = {
    "Content-Type": "application/json",
    "Accept": "application/json, */*;q=0.5",
    "Token": '{"uid":"","timestamp":0,"token":"","client":"semsPlusWeb","version":"","language":"en"}',
}


@dataclass(frozen=True, slots=True)
class SemsStation:
    """A station available to the authenticated SEMS account."""

    station_id: str
    name: str


class SemsApi:
    """Interface to the SEMS API."""

    def __init__(
        self,
        hass: HomeAssistant,
        username: str,
        password: str,
        region: str = DEFAULT_SEMS_REGION,
    ) -> None:
        """Init dummy hub."""
        try:
            self._region = SEMS_REGIONS[region]
        except KeyError as err:
            raise ValueError(f"Unsupported SEMS region: {region}") from err
        self._hass = hass
        self._username = username
        self._password = password
        self._token: dict[str, Any] | None = None

    def test_authentication(self) -> bool:
        """Test if we can authenticate with the host."""
        try:
            self._token = self.getLoginToken(self._username, self._password)
        except (AttributeError, KeyError, TypeError, ValueError) as exception:
            _LOGGER.exception("SEMS Authentication exception: %s", exception)
            return False
        else:
            return self._token is not None

    def _make_http_request(
        self,
        url: str,
        headers: dict[str, str],
        data: str | None = None,
        json_data: dict[str, Any] | None = None,
        operation_name: str = "HTTP request",
        validate_code: bool = True,
        method: str = "POST",
    ) -> dict[str, Any] | None:
        """Make a generic HTTP request with error handling and optional code validation."""
        try:
            _LOGGER.debug("SEMS - Making %s to %s", operation_name, url)
            response = requests.request(
                method.upper(),
                url,
                headers=headers,
                data=data,
                json=json_data,
                timeout=_RequestTimeout,
            )

            _LOGGER.debug("%s Response: %s", operation_name, response)
            # _LOGGER.debug("%s Response text: %s", operation_name, response.text)

            response.raise_for_status()
            json_response: dict[str, Any] = response.json()
            response_code = json_response.get("code")

            if self._is_sensitive_operation(operation_name):
                _LOGGER.debug(
                    "SEMS - %s response payload: %s",
                    operation_name,
                    redact_for_log(json_response),
                )

            _LOGGER.debug(
                "SEMS - %s response summary: code=%s msg=%s description=%s api=%s has_data=%s",
                operation_name,
                response_code,
                json_response.get("msg"),
                json_response.get("description"),
                json_response.get("api"),
                json_response.get("data") not in (None, "", [], {}),
            )

            if str(response_code) == _RateLimitCode:
                raise SemsRateLimitedError(
                    retry_after=_RateLimitRetryAfterSeconds,
                    message=(
                        f"{operation_name} returned rate-limit code {_RateLimitCode}"
                    ),
                )

            # Always check for authorization errors, regardless of validate_code
            error_msg = json_response.get("msg", "Unknown error")
            is_auth_error = (
                str(response_code) == "100002" or "authorization" in error_msg.lower()
            )

            # Validate response code if requested
            if validate_code:
                if response_code not in _SuccessCodes:
                    if is_auth_error:
                        _LOGGER.debug(
                            "%s - Authorization expired (code: %s): %s. Will retry with fresh token.",
                            operation_name,
                            response_code,
                            error_msg,
                        )
                    else:
                        _LOGGER.error(
                            "%s failed with code: %s, message: %s",
                            operation_name,
                            response_code,
                            error_msg,
                        )
                    return None
            elif is_auth_error:
                # Even if not validating codes, we must retry on auth errors
                _LOGGER.debug(
                    "%s - Authorization expired (code: %s): %s. Will retry with fresh token.",
                    operation_name,
                    response_code,
                    error_msg,
                )
                return None

            return json_response

        except requests.HTTPError as exception:
            if (response := exception.response) is not None:
                if self._is_sensitive_operation(operation_name):
                    _LOGGER.error(
                        "Unable to complete %s: status=%s url=%s (response body redacted)",
                        operation_name,
                        response.status_code,
                        response.url,
                    )
                else:
                    _LOGGER.error(
                        "Unable to complete %s: status=%s url=%s body=%s",
                        operation_name,
                        response.status_code,
                        response.url,
                        response.text,
                    )
            else:
                _LOGGER.error("Unable to complete %s: %s", operation_name, exception)
            raise
        except (requests.RequestException, ValueError, KeyError) as exception:
            _LOGGER.error("Unable to complete %s: %s", operation_name, exception)
            raise

    def _is_sensitive_operation(self, operation_name: str) -> bool:
        """Return True if the operation name indicates it handles sensitive credentials."""
        return "login" in operation_name.lower()

    def _hash_password_for_new_login(self, password: str) -> str:
        """Return the SEMS+ password encoding."""
        # MD5 is required by the SEMS+ API protocol; usedforsecurity=False avoids
        # failures on FIPS-enabled systems where MD5 is disabled for security use.
        md5_password = hashlib.md5(
            password.encode("utf-8"), usedforsecurity=False
        ).hexdigest()
        return base64.b64encode(md5_password.encode("utf-8")).decode("utf-8")

    def _is_powerstation_route(self, url_part: str) -> bool:
        """Return whether the route should use the legacy PowerStation host."""
        return url_part.startswith("/PowerStation") or url_part.startswith(
            "/v3/PowerStation"
        )

    def _extract_gateway_region(self, api_base: str) -> str | None:
        """Return the SEMS region prefix from a gateway API base."""
        host = api_base.split("//", 1)[-1].split("/", 1)[0]
        if host.endswith("-gateway.semsportal.com"):
            return host.removesuffix("-gateway.semsportal.com") or None

        if host.endswith(".semsportal.com"):
            return host.split(".", 1)[0] or None

        return None

    def _normalize_powerstation_api_base(self, api_base: str, url_part: str) -> str:
        """Return the effective API base for PowerStation requests."""
        if not self._is_powerstation_route(url_part):
            return api_base

        if "/web/sems" not in api_base and "/sems/" not in api_base:
            return api_base

        region = None
        if isinstance(self._token, dict) and isinstance(self._token.get("region"), str):
            region = self._token["region"] or None
        if region is None:
            region = self._extract_gateway_region(api_base)

        if region:
            rewritten_base = f"https://{region}.semsportal.com/api"
            _LOGGER.debug(
                "SEMS - Rewriting API base from %s to %s for %s",
                api_base,
                rewritten_base,
                url_part,
            )
            return rewritten_base

        _LOGGER.debug(
            "SEMS - Rewriting API base from %s to fallback %s for %s",
            api_base,
            self._region.powerstation_api_url,
            url_part,
        )
        return self._region.powerstation_api_url

    def _get_authenticated_request_context(
        self,
        url_part: str,
        renewToken: bool,
        operation_name: str,
    ) -> tuple[str, dict[str, str]] | None:
        """Return the request URL and headers for an authenticated call."""
        token = self._token

        if token is None or renewToken:
            _LOGGER.debug(
                "API token not set (%s) or new token requested (%s), fetching",
                redact_for_log(token),
                renewToken,
            )
            self._token = self.getLoginToken(self._username, self._password)
            token = self._token

        if token is None:
            _LOGGER.error("Failed to obtain API token")
            return None

        api_base = self._normalize_powerstation_api_base(token["api"], url_part)
        api_url = api_base + url_part
        headers = self._build_authenticated_headers(token)

        _LOGGER.debug(
            "SEMS - %s request context: api_base=%s effective_api_base=%s url_part=%s token=%s",
            operation_name,
            token.get("api"),
            api_base,
            url_part,
            redact_for_log(token),
        )
        return api_url, headers

    def _build_authenticated_headers(
        self,
        token_data: dict[str, Any],
    ) -> dict[str, str]:
        """Build request headers for authenticated API calls."""
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "token": json.dumps(token_data),
        }

        if token_data.get("client") == "semsPlusWeb":
            headers["X-Signature"] = self._generate_signature(token_data)

        return headers

    def _generate_signature(self, token_data: dict[str, Any]) -> str:
        epoch_ms = round(time.time() * 1000)
        digest = hashlib.sha256(
            f"{epoch_ms}@{token_data.get('uid', '')}@{token_data.get('token', '')}".encode()
        ).hexdigest()
        sig = f"{digest}@{epoch_ms}"
        return base64.b64encode(sig.encode()).decode()

    def _resolve_login_api_url(
        self,
        json_response: dict[str, Any],
        token_data: dict[str, Any],
        fallback_api_url: str | None,
    ) -> str | None:
        """Resolve API URL from login response with optional fallback."""
        api_url = (
            json_response.get("api")
            if isinstance(json_response.get("api"), str)
            else token_data.get("api")
        )
        if isinstance(api_url, str) and api_url:
            return api_url

        if fallback_api_url is None:
            _LOGGER.error(
                "SEMS %s login response missing api field: keys=%s",
                "web",
                list(json_response.keys()),
            )
            return None

        _LOGGER.debug(
            "SEMS %s login response missing api field, falling back to %s",
            "web",
            fallback_api_url,
        )
        return fallback_api_url

    def _extract_login_token(
        self,
        json_response: dict[str, Any] | None,
        operation_name: str,
        fallback_api_url: str | None = None,
    ) -> dict[str, Any] | None:
        """Normalize a login response into the token payload expected elsewhere."""
        if json_response is None:
            return None

        code = json_response.get("code")
        if code not in _SuccessCodes:
            _LOGGER.warning(
                "SEMS %s login rejected during %s: code=%s msg=%s description=%s api=%s data_type=%s response=%s",
                "web",
                operation_name,
                code,
                json_response.get("msg"),
                json_response.get("description"),
                json_response.get("api"),
                type(json_response.get("data")).__name__,
                redact_for_log(json_response),
            )
            return None

        token_data = json_response.get("data")
        if not isinstance(token_data, dict) or not token_data:
            _LOGGER.error(
                "SEMS+ login response data was missing or invalid: data_type=%s, keys=%s",
                type(token_data).__name__,
                list(json_response.keys()),
            )
            return None

        api_url = self._resolve_login_api_url(
            json_response,
            token_data,
            fallback_api_url,
        )
        if api_url is None:
            return None

        token_dict = dict(token_data)
        token_dict["api"] = api_url

        if not token_dict.get("token"):
            _LOGGER.warning(
                "SEMS+ login response missing valid token field - incomplete token received",
            )
            return None

        _LOGGER.debug(
            "SEMS - API Token received via %s login: %s",
            "web",
            redact_for_log(token_dict),
        )

        return token_dict

    def _get_new_login_token(
        self, userName: str, password: str
    ) -> dict[str, Any] | None:
        """Get a token from the SEMS+ login endpoint."""
        operation_name = "SEMS+ Web login API call"
        _LOGGER.debug("SEMS - Trying %s", operation_name)
        login_data = {
            "account": userName,
            "pwd": self._hash_password_for_new_login(password),
            "agreement": 1,
            "isChinese": False,
            "isLocal": False,
        }
        headers = {
            **_NewSEMSPlusWebLoginHeaders,
            "X-Signature": self._generate_signature({}),
        }

        json_response = self._make_http_request(
            self._region.login_url,
            headers,
            json_data=login_data,
            operation_name=operation_name,
            validate_code=False,
        )
        return self._extract_login_token(
            json_response,
            operation_name,
            self._region.gateway_api_url,
        )

    def getLoginToken(self, userName: str, password: str) -> dict[str, Any] | None:
        """Get a login token from the Australian SEMS+ API."""
        _LOGGER.debug(
            "SEMS+ authentication attempt: account=%s endpoint=%s",
            redact_value(userName),
            self._region.login_url,
        )
        try:
            token = self._get_new_login_token(userName, password)
            if token is not None:
                return token

            _LOGGER.error("Unable to authenticate with Australian SEMS+ API")
            return None

        except (requests.RequestException, ValueError, KeyError) as exception:
            _LOGGER.error(
                "Unable to fetch login token from Australian SEMS+ API: %s",
                exception,
            )
            return None

    def _make_api_call(
        self,
        url_part: str,
        data: str | None = None,
        renewToken: bool = False,
        maxTokenRetries: int = 2,
        operation_name: str = "API call",
        method: str = "POST",
        retry_on_api_error: bool = True,
        return_raw_response: bool = False,
    ) -> Any | None:
        """Make a generic API call with token management and retry logic."""
        _LOGGER.debug("SEMS - Making %s", operation_name)
        if maxTokenRetries <= 0:
            _LOGGER.error(
                "SEMS - %s failed: Maximum token refresh attempts exceeded. "
                "Check your SEMS credentials or wait a moment before retrying.",
                operation_name,
            )
            raise OutOfRetries

        context = self._get_authenticated_request_context(
            url_part,
            renewToken,
            operation_name,
        )
        if context is None:
            return None

        api_url, headers = context

        try:
            json_response: dict[str, Any] | None = self._make_http_request(
                api_url,
                headers,
                data=data,
                method=method,
                operation_name=operation_name,
                validate_code=retry_on_api_error,
            )

            # _make_http_request already validated the response, so if we get here, it's successful
            if json_response is None:
                # Response validation failed in _make_http_request (likely auth error)
                _LOGGER.info(
                    "SEMS - %s encountered an error. Refreshing authentication token and retrying (%s attempts remaining)...",
                    operation_name,
                    maxTokenRetries - 1,
                )
                return self._make_api_call(
                    url_part,
                    data,
                    True,
                    maxTokenRetries - 1,
                    operation_name,
                    method,
                    retry_on_api_error,
                    return_raw_response,
                )

            # Response is valid, return the data
            if return_raw_response:
                return json_response
            return json_response.get("data", {})

        except SemsRateLimitedError as exception:
            _LOGGER.debug(
                "SEMS - Propagating rate limit from %s to coordinator: retry_after=%s",
                operation_name,
                exception.retry_after,
            )
            raise
        except (requests.RequestException, ValueError, KeyError) as exception:
            _LOGGER.error("Unable to complete %s: %s", operation_name, exception)
            return None

    def get_stations(
        self, renew_token: bool = False, max_token_retries: int = 2
    ) -> list[SemsStation]:
        """Return all named stations available to the authenticated account."""
        stations: list[SemsStation] = []
        seen_station_ids: set[str] = set()
        page = 1

        while True:
            result = self._make_api_call(
                _StationsPageURLPart,
                data=json.dumps({"current": page, "size": _StationsPageSize}),
                renewToken=renew_token,
                maxTokenRetries=max_token_retries,
                operation_name="getStations API call",
            )
            if not isinstance(result, dict):
                return []

            raw_stations = result.get("dataList")
            if not isinstance(raw_stations, list):
                return []

            added_on_page = 0
            for raw_station in raw_stations:
                if not isinstance(raw_station, dict):
                    continue
                station_id = raw_station.get("id")
                name = raw_station.get("name")
                if not isinstance(station_id, str) or not station_id:
                    continue
                if station_id in seen_station_ids:
                    continue

                seen_station_ids.add(station_id)
                stations.append(
                    SemsStation(
                        station_id=station_id,
                        name=name.strip()
                        if isinstance(name, str) and name.strip()
                        else f"Station {station_id}",
                    )
                )
                added_on_page += 1

            total = result.get("total")
            if (
                not raw_stations
                or added_on_page == 0
                or not isinstance(total, int)
                or len(stations) >= total
            ):
                return stations

            page += 1

    def getData(
        self, powerStationId: str, renewToken: bool = False, maxTokenRetries: int = 2
    ) -> dict[str, Any]:
        """Get the latest data from the SEMS API and updates the state."""
        data = '{"powerStationId":"' + powerStationId + '"}'
        result = self._make_api_call(
            _PowerStationURLPart,
            data=data,
            renewToken=renewToken,
            maxTokenRetries=maxTokenRetries,
            operation_name="getData API call",
        )
        return result if isinstance(result, dict) else {}

    def getMqttConfig(
        self, renewToken: bool = False, maxTokenRetries: int = 2
    ) -> dict[str, Any]:
        """Get the short-lived MQTT-over-WebSocket connection configuration."""
        result = self._make_api_call(
            _MqttConfigURLPart,
            method="GET",
            renewToken=renewToken,
            maxTokenRetries=maxTokenRetries,
            operation_name="getMqttConfig API call",
        )
        config = result if isinstance(result, dict) else {}
        _LOGGER.debug("SEMS MQTT configuration: %s", redact_for_log(config))
        return config

    def enableSecondData(
        self, powerStationId: str, renewToken: bool = False, maxTokenRetries: int = 2
    ) -> bool:
        """Enable second-data (MQTT live updates) for a power station."""
        result = self._make_api_call(
            f"{_EnableSecondDataURLPart}?stationId={powerStationId}",
            method="GET",
            renewToken=renewToken,
            maxTokenRetries=maxTokenRetries,
            operation_name="enableSecondData API call",
            retry_on_api_error=False,
            return_raw_response=True,
        )

        if not isinstance(result, dict):
            _LOGGER.warning(
                "Unable to enable second-data for station %s: empty response",
                redact_for_log(powerStationId),
            )
            return False

        if result.get("code") in _SuccessCodes:
            _LOGGER.debug(
                "Second-data enabled for station %s", redact_for_log(powerStationId)
            )
            return True

        _LOGGER.warning(
            "Failed to enable second-data for station %s: code=%s msg=%s description=%s",
            redact_for_log(powerStationId),
            result.get("code"),
            result.get("msg"),
            result.get("description"),
        )
        return False


class OutOfRetries(exceptions.HomeAssistantError):
    """Error to indicate too many error attempts."""


class SemsRateLimitedError(exceptions.HomeAssistantError):
    """Error to indicate the SEMS API requested retry with backoff."""

    def __init__(self, retry_after: int, message: str = "SEMS API rate limited"):
        """Initialize rate limit exception."""
        super().__init__(message)
        self.retry_after = retry_after
