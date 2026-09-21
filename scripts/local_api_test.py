#!/usr/bin/env python3
"""Capture SEMS REST and MQTT data without running Home Assistant."""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import logging
import os
import ssl
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiomqtt
from dotenv import load_dotenv

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPOSITORY_ROOT))

from custom_components.sems_au.const import (  # noqa: E402
    DEFAULT_SEMS_REGION,
)
from custom_components.sems_au.sems_api import SemsApi  # noqa: E402
from custom_components.sems_au.sems_mqtt import (  # noqa: E402
    SemsMqttConfig,
    decode_mqtt_payload,
)

_RECONNECT_DELAY = 5


class MqttPacketCapture:
    """Capture MQTT packets for comparison with browser traffic."""

    def __init__(self):
        self.sent_packets = []
        self.received_packets = []

    def log_sent(self, label: str, data: bytes):
        """Log a packet we're sending."""
        self.sent_packets.append(
            {
                "timestamp": datetime.now(UTC).isoformat(),
                "label": label,
                "hex": data.hex(),
                "base64": base64.b64encode(data).decode("ascii"),
                "length": len(data),
            }
        )
        logging.debug("SENT [%s] %d bytes: %s", label, len(data), data.hex()[:80])

    def log_received(self, label: str, data: bytes):
        """Log a packet we received."""
        self.received_packets.append(
            {
                "timestamp": datetime.now(UTC).isoformat(),
                "label": label,
                "hex": data.hex(),
                "base64": base64.b64encode(data).decode("ascii"),
                "length": len(data),
            }
        )
        logging.debug("RECV [%s] %d bytes: %s", label, len(data), data.hex()[:80])

    def save_to_file(self, output_dir: Path):
        """Save packet capture to JSON files for comparison."""
        if self.sent_packets or self.received_packets:
            timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")

            if self.sent_packets:
                sent_file = output_dir / f"{timestamp}_mqtt_sent_packets.json"
                sent_file.write_text(
                    json.dumps(self.sent_packets, indent=2), encoding="utf-8"
                )
                logging.info("Sent packets: %s", sent_file)

            if self.received_packets:
                recv_file = output_dir / f"{timestamp}_mqtt_received_packets.json"
                recv_file.write_text(
                    json.dumps(self.received_packets, indent=2), encoding="utf-8"
                )
                logging.info("Received packets: %s", recv_file)


class _ColoredFormatter(logging.Formatter):
    """Custom formatter with colored output for console."""

    # ANSI color codes
    COLORS = {
        "DEBUG": "\033[90m",  # Dark grey
        "INFO": "\033[0m",  # Default
        "WARNING": "\033[93m",  # Bright yellow
        "ERROR": "\033[91m",  # Bright red
        "CRITICAL": "\033[41m\033[37m",  # Red background with white text
    }
    RESET = "\033[0m"
    ITALIC = "\033[3m"
    BOLD = "\033[1m"

    def format(self, record: logging.LogRecord) -> str:
        """Format a log record with colors."""
        levelname = record.levelname
        color = self.COLORS.get(levelname, self.RESET)

        # Add styling
        if levelname == "DEBUG":
            # Grey italic for debug
            formatted_level = f"{color}{self.ITALIC}{levelname}{self.RESET}"
            formatted_msg = f"{color}{self.ITALIC}{record.getMessage()}{self.RESET}"
        elif levelname in ("ERROR", "CRITICAL"):
            # Bold red for errors
            formatted_level = f"{color}{self.BOLD}{levelname}{self.RESET}"
            formatted_msg = f"{color}{self.BOLD}{record.getMessage()}{self.RESET}"
        elif levelname == "WARNING":
            # Bold yellow for warnings
            formatted_level = f"{color}{self.BOLD}{levelname}{self.RESET}"
            formatted_msg = f"{color}{record.getMessage()}{self.RESET}"
        else:
            formatted_level = f"{color}{levelname}{self.RESET}"
            formatted_msg = f"{color}{record.getMessage()}{self.RESET}"

        # Build the formatted output
        return f"{record.asctime} - {record.name} - {formatted_level} - {formatted_msg}"


class MinimalHass:
    """Minimal Home Assistant stub for local API testing."""

    async def async_add_executor_job(self, func, *args):
        """Execute a synchronous function in a worker thread."""
        return await asyncio.to_thread(func, *args)


def _parse_arguments() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--duration",
        type=float,
        help="Stop after this many seconds instead of waiting for Ctrl+C",
    )
    parser.add_argument(
        "--api-only",
        action="store_true",
        help="Run only API validations (REST snapshots and energy storage data) without MQTT capture",
    )
    return parser.parse_args()


def _station_id_from_response(value: Any) -> str | None:
    """Return the first station ID from the supported API response shapes."""
    if isinstance(value, str) and value:
        return value
    if isinstance(value, list) and value:
        first = value[0]
        return str(first) if first else None
    return None


def _capture_record(topic: str, payload: bytes) -> dict[str, Any]:
    """Build a lossless, analysis-friendly record for one MQTT message."""
    record: dict[str, Any] = {
        "received_at": datetime.now(UTC).isoformat(),
        "topic": topic,
    }
    try:
        raw_text = payload.decode("utf-8")
    except UnicodeDecodeError:
        record.update(
            {
                "raw_encoding": "base64",
                "raw_payload": base64.b64encode(payload).decode("ascii"),
                "parsed_payload": None,
            }
        )
    else:
        record.update(
            {
                "raw_encoding": "utf-8",
                "raw_payload": raw_text,
                "parsed_payload": decode_mqtt_payload(payload),
            }
        )
    return record


async def _capture_mqtt(
    api: SemsApi,
    station_id: str,
    output_file: Path,
    log_dir: Path,
    duration: float | None,
) -> int:
    """Stream MQTT messages to the console and a JSON Lines capture file."""
    import logging as stdlib_logging

    # paho-mqtt's enable_logger() only wires up whatever logger object is
    # passed to aiomqtt.Client(logger=...) - it does not use a fixed logger
    # name, so setting the "aiomqtt" logger to DEBUG has no effect on its own.
    protocol_logger = stdlib_logging.getLogger("sems_mqtt.protocol")
    protocol_logger.setLevel(stdlib_logging.DEBUG)

    # Also enable paho-mqtt's root logger (used by enable_logger)
    paho_logger = stdlib_logging.getLogger("paho.mqtt.client")
    paho_logger.setLevel(stdlib_logging.DEBUG)

    topic = f"/goodwe/second-data/station/{station_id}"
    deadline = asyncio.get_running_loop().time() + duration if duration else None
    message_count = 0
    packet_capture = MqttPacketCapture()

    with output_file.open("a", encoding="utf-8") as capture:
        while deadline is None or asyncio.get_running_loop().time() < deadline:
            try:
                enabled = await asyncio.to_thread(
                    api.enableSecondData,
                    station_id,
                )
                if not enabled:
                    raise ValueError("SEMS second-data enable call failed")

                config_data = await asyncio.to_thread(api.getMqttConfig)
                config = SemsMqttConfig.from_api(config_data)
                tls_context = ssl.create_default_context() if config.use_tls else None

                # Warm up the session by making a REST call first
                # This ensures the broker recognizes us as an authenticated session
                logging.info("=== Session warm-up: Making initial REST API call ===")
                try:
                    await asyncio.to_thread(api.getData, station_id)
                    logging.info("✓ REST call complete, session activated")
                except Exception as warmup_err:
                    logging.warning(
                        "⚠ Session warm-up call failed: %s (continuing anyway)",
                        warmup_err,
                    )

                # Brief delay to let session settle
                await asyncio.sleep(1)

                # Stage 1/3: about to open the WebSocket and send MQTT CONNECT
                logging.info("=== Stage 1/3: MQTT CONNECT (sending to broker) ===")
                logging.info("  client_id: %s", config.client_id)
                logging.info("  username: %s", "***" if config.username else "NONE")
                logging.info("  keepalive: 60s")
                logging.info("  protocol: MQTT 3.1.1")
                logging.info("  websocket_origin: %s", config.websocket_origin)
                logging.info("===")

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
                    keepalive=60,
                    logger=protocol_logger,
                ) as client:
                    # Stage 2/3: WebSocket handshake + CONNECT/CONNACK succeeded
                    logging.info(
                        "=== Stage 2/3: CONNACK received, MQTT session established ==="
                    )

                    # Capture CONNECT config for packet inspection
                    connect_info = {
                        "client_id": config.client_id,
                        "username_present": bool(config.username),
                        "password_present": bool(config.password),
                        "keepalive": 60,
                        "clean_session": True,
                        "protocol": "MQTT 3.1.1",
                    }
                    packet_capture.log_sent(
                        "CONNECT", json.dumps(connect_info).encode()
                    )

                    # Stage 3/3: about to send SUBSCRIBE and wait for SUBACK
                    logging.info(
                        "=== Stage 3/3: MQTT SUBSCRIBE (sending to broker) ==="
                    )
                    logging.info("  topic: %s", topic)
                    logging.info("  qos: 0")
                    logging.info("===")

                    subscribe_info = {
                        "topic": topic,
                        "qos": 0,
                    }
                    packet_capture.log_sent(
                        "SUBSCRIBE", json.dumps(subscribe_info).encode()
                    )

                    await client.subscribe(topic, qos=0)

                    logging.info(
                        "=== SUBACK received: connected to %s:%s and subscribed to %s ===",
                        config.hostname,
                        config.port,
                        topic,
                    )
                    logging.debug(
                        "Broker details: hostname=%s, port=%d, "
                        "use_tls=%s, websocket_path=%s, client_id=%s",
                        config.hostname,
                        config.port,
                        config.use_tls,
                        config.websocket_path,
                        config.client_id,
                    )

                    logging.info("=== Post-SUBACK: Waiting for device to publish ===")

                    async def consume_messages() -> None:
                        nonlocal message_count
                        logging.debug("Starting message consumption loop")
                        logging.debug("Waiting for messages on topic: %s", topic)
                        message_idx = 0
                        start_time = asyncio.get_running_loop().time()

                        async for message in client.messages:
                            elapsed = asyncio.get_running_loop().time() - start_time

                            # Log if this is the first message (indicates handshake + device sending)
                            if message_idx == 0:
                                logging.info(
                                    "First MQTT message received after %.1f seconds",
                                )

                            message_idx += 1
                            payload_bytes = bytes(message.payload)

                            # Log full packet hex dump for comparison with browser traffic
                            logging.debug(
                                "MQTT message %d: topic=%s, length=%d, hex=%s",
                                message_idx,
                                str(message.topic),
                                len(payload_bytes),
                                payload_bytes[:50].hex(),
                            )

                            try:
                                record = _capture_record(
                                    str(message.topic), payload_bytes
                                )
                                capture.write(
                                    json.dumps(record, ensure_ascii=False, default=str)
                                    + "\n"
                                )
                                capture.flush()
                                message_count += 1
                                logging.info(
                                    "MQTT message %s topic=%s payload=%s",
                                    message_count,
                                    record["topic"],
                                    json.dumps(
                                        record["parsed_payload"],
                                        ensure_ascii=False,
                                        default=str,
                                    ),
                                )
                            except Exception as decode_err:
                                logging.error(
                                    "Failed to decode MQTT message %d: %s. "
                                    "Raw bytes (first 100): %r, hex: %s",
                                    message_idx,
                                    decode_err,
                                    payload_bytes[:100],
                                    payload_bytes[:50].hex(),
                                    exc_info=False,
                                )

                        if message_idx == 0:
                            elapsed = asyncio.get_running_loop().time() - start_time
                            logging.warning(
                                "No MQTT messages received after %.1f seconds",
                                elapsed,
                            )

                    if deadline is None:
                        await consume_messages()
                    else:
                        async with asyncio.timeout_at(deadline):
                            await consume_messages()
            except TimeoutError:
                logging.info("Deadline reached; stopping capture")
                break
            except (aiomqtt.MqttError, ValueError) as err:
                logging.warning(
                    "MQTT connection failed (%s); retrying in %s seconds",
                    err,
                    _RECONNECT_DELAY,
                )
                if deadline is not None:
                    remaining = deadline - asyncio.get_running_loop().time()
                    if remaining <= 0:
                        break
                    await asyncio.sleep(min(_RECONNECT_DELAY, remaining))
                else:
                    await asyncio.sleep(_RECONNECT_DELAY)
            except Exception as err:
                logging.error(
                    "Unexpected error in MQTT capture: %s", err, exc_info=True
                )
                raise

        if message_count == 0:
            logging.warning(
                "No MQTT messages received. This usually means no data is currently being "
                "published on the topic. The connection and subscription succeeded, but the "
                "device may not be actively sending data at this moment. Try running again "
                "or checking the device status via the REST API."
            )

    # Save packet captures for comparison with browser
    packet_capture.save_to_file(log_dir)

    return message_count


async def _async_main(args: argparse.Namespace) -> None:
    """Run the local REST snapshot and MQTT capture."""
    load_dotenv(REPOSITORY_ROOT / ".env")
    log_dir = REPOSITORY_ROOT / "logs"
    log_dir.mkdir(exist_ok=True)
    timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    log_file = log_dir / f"{timestamp}_local_test.log"
    mqtt_file = log_dir / f"{timestamp}_mqtt_messages.jsonl"

    # Set up logging with colored console output and plain file output
    logger = logging.getLogger()
    logger.setLevel(logging.DEBUG)

    # File handler: plain format
    file_handler = logging.FileHandler(log_file)
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
    )
    logger.addHandler(file_handler)

    # Console handler: colored format
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.DEBUG)
    console_handler.setFormatter(_ColoredFormatter())
    logger.addHandler(console_handler)

    username = os.getenv("SEMS_USERNAME")
    password = os.getenv("SEMS_PASSWORD")
    if not username or not password:
        raise SystemExit("SEMS_USERNAME and SEMS_PASSWORD must be set in .env")

    logging.info("Authenticating SEMS account %s", username)
    api = SemsApi(MinimalHass(), username, password)
    authenticated = await asyncio.to_thread(api.test_authentication)
    if not authenticated:
        raise SystemExit("Authentication failed")

    power_stations = await asyncio.to_thread(api.getPowerStationIds)
    station_list_file = log_dir / f"{timestamp}_raw_getPowerStationIds.json"
    station_list_file.write_text(
        json.dumps({"data": power_stations}, indent=2), encoding="utf-8"
    )

    station_id = os.getenv("SEMS_POWER_STATION_ID") or _station_id_from_response(
        power_stations
    )
    if not station_id:
        raise SystemExit(f"No usable power station ID returned: {power_stations!r}")

    logging.info("Fetching MQTT config for CONNECT packet construction")
    mqtt_config_full = await asyncio.to_thread(api.getMqttConfig)
    mqtt_config = SemsMqttConfig.from_api(mqtt_config_full)
    mqtt_config_file = log_dir / f"{timestamp}_mqtt_config.json"
    mqtt_config_file.write_text(
        json.dumps(
            {
                "raw_response": mqtt_config_full,
                "parsed_config": {
                    "hostname": mqtt_config.hostname,
                    "port": mqtt_config.port,
                    "websocket_path": mqtt_config.websocket_path,
                    "websocket_origin": mqtt_config.websocket_origin,
                    "use_tls": mqtt_config.use_tls,
                    "client_id": mqtt_config.client_id,
                    "username_length": len(mqtt_config.username),
                    "password_length": len(mqtt_config.password),
                    "username_prefix": mqtt_config.username[:50],
                    "password_prefix": base64.b64encode(
                        mqtt_config.password.encode()[:50]
                    ).decode(),
                },
            },
            indent=2,
            default=str,
        ),
        encoding="utf-8",
    )
    logging.info("MQTT config saved to: %s", mqtt_config_file)

    logging.info("Fetching initial REST snapshot for %s", station_id)
    monitoring_data = await asyncio.to_thread(api.getData, station_id)
    if not monitoring_data:
        raise SystemExit("Failed to fetch initial monitoring data")
    monitoring_file = log_dir / f"{timestamp}_raw_monitoring_data.json"
    monitoring_file.write_text(json.dumps(monitoring_data, indent=2), encoding="utf-8")

    logging.info("Initial REST snapshot: %s", monitoring_file)

    # Capture energy storage and battery data if available
    inverters = monitoring_data.get("inverter", [])
    if isinstance(inverters, list):
        energy_storage_file = log_dir / f"{timestamp}_raw_energy_storage.json"
        battery_functions_file = log_dir / f"{timestamp}_raw_battery_functions.json"
        charging_states_file = log_dir / f"{timestamp}_raw_charging_states.json"

        energy_storage_data = {}
        battery_functions_data = {}
        charging_states_data = {}

        for inverter in inverters:
            inverter_full = inverter.get("invert_full", {})
            serial_number = inverter_full.get("sn")
            if not serial_number:
                continue

            logging.info("Fetching energy storage data for inverter %s", serial_number)
            try:
                cabinets = await asyncio.to_thread(
                    api.getEnergyStorageIntegratedCabinets,
                    station_id,
                    serial_number,
                )
                energy_storage_data[serial_number] = cabinets
                logging.info(
                    "  Energy storage cabinets: %s", len(cabinets) if cabinets else 0
                )
                # Add delay to avoid rate limiting
                await asyncio.sleep(2)
            except Exception as err:
                logging.warning(
                    "  Failed to fetch energy storage for %s: %s",
                    serial_number,
                    err,
                )
                # Still add delay even on error
                await asyncio.sleep(2)

            # Fetch battery functions if energy storage exists
            if energy_storage_data.get(serial_number):
                for idx, cabinet in enumerate(energy_storage_data[serial_number]):
                    cabinet_sn = cabinet.get("sn")
                    if not cabinet_sn:
                        continue
                    logging.info(
                        "Fetching battery functions for cabinet %s (index %d)",
                        cabinet_sn,
                        idx,
                    )
                    try:
                        functions = await asyncio.to_thread(
                            api.getBatteryGeneralFunctions,
                            cabinet_sn,
                            idx,
                        )
                        battery_functions_data[cabinet_sn] = functions
                        logging.info("  Battery functions: %s", functions)
                    except Exception as err:
                        logging.warning(
                            "  Failed to fetch battery functions for %s: %s",
                            cabinet_sn,
                            err,
                        )
                    # Add delay between battery function calls
                    await asyncio.sleep(2)

                    logging.info(
                        "Fetching immediate charging states for cabinet %s (index %d)",
                        cabinet_sn,
                        idx,
                    )
                    try:
                        states = await asyncio.to_thread(
                            api.getBatteryImmediateChargingStates,
                            cabinet_sn,
                            idx,
                        )
                        charging_states_data[cabinet_sn] = states
                        logging.info("  Charging states: %s", states)
                    except Exception as err:
                        logging.warning(
                            "  Failed to fetch charging states for %s: %s",
                            cabinet_sn,
                            err,
                        )
                    # Add delay after charging states call
                    await asyncio.sleep(2)

        if energy_storage_data:
            energy_storage_file.write_text(
                json.dumps(energy_storage_data, indent=2, default=str),
                encoding="utf-8",
            )
            logging.info("Energy storage snapshot: %s", energy_storage_file)

        if battery_functions_data:
            battery_functions_file.write_text(
                json.dumps(battery_functions_data, indent=2, default=str),
                encoding="utf-8",
            )
            logging.info("Battery functions snapshot: %s", battery_functions_file)

        if charging_states_data:
            charging_states_file.write_text(
                json.dumps(charging_states_data, indent=2, default=str),
                encoding="utf-8",
            )
            logging.info("Charging states snapshot: %s", charging_states_file)

    if not args.api_only:
        logging.info("MQTT capture: %s", mqtt_file)
        logging.info(
            "Streaming live messages from region %s; press Ctrl+C to stop",
            DEFAULT_SEMS_REGION,
        )
        # Add delay before MQTT to avoid rate limiting after REST calls
        logging.info("Waiting 3 seconds before MQTT connection to let broker settle...")
        await asyncio.sleep(3)
        message_count = await _capture_mqtt(
            api, station_id, mqtt_file, log_dir, args.duration
        )
        logging.info("Capture complete: %s MQTT messages written", message_count)
    else:
        logging.info("Skipping MQTT capture (--api-only mode)")


def main() -> None:
    """Run the local capture and handle an interactive stop cleanly."""
    try:
        asyncio.run(_async_main(_parse_arguments()))
    except KeyboardInterrupt:
        logging.info("Capture stopped by user; all received messages were flushed")


if __name__ == "__main__":
    main()
