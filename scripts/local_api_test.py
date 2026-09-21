#!/usr/bin/env python3
"""Exercise the read-only SEMS station flow without running Home Assistant."""

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

from custom_components.sems_au.const import DEFAULT_SEMS_REGION  # noqa: E402
from custom_components.sems_au.sems_api import SemsApi, SemsStation  # noqa: E402
from custom_components.sems_au.sems_mqtt import (  # noqa: E402
    SemsMqttConfig,
    decode_mqtt_payload,
    normalize_mqtt_powerflow_payload,
)

_LOG_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"


class MinimalHass:
    """Provide the executor method used by the synchronous API client."""

    async def async_add_executor_job(self, func, *args):
        """Execute a synchronous function in a worker thread."""
        return await asyncio.to_thread(func, *args)


def _parse_arguments() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--station-id",
        help="Select a discovered station without an interactive prompt",
    )
    parser.add_argument(
        "--first-station",
        action="store_true",
        help="Select the first discovered station for non-interactive testing",
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=30,
        help="Capture MQTT messages for this many seconds (default: 30)",
    )
    parser.add_argument(
        "--api-only",
        action="store_true",
        help="Stop after station discovery and the REST snapshot",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging",
    )
    return parser.parse_args()


def _configure_logging(log_file: Path, debug: bool) -> None:
    """Configure matching console and file logs."""
    level = logging.DEBUG if debug else logging.INFO
    logging.basicConfig(
        level=level,
        format=_LOG_FORMAT,
        handlers=[
            logging.FileHandler(log_file),
            logging.StreamHandler(),
        ],
        force=True,
    )


def _station_record(station: SemsStation) -> dict[str, str]:
    """Return a serializable station discovery record."""
    return {"id": station.station_id, "name": station.name}


def _select_station(
    stations: list[SemsStation],
    requested_station_id: str | None,
    select_first: bool,
) -> SemsStation:
    """Select one discovered station explicitly."""
    if not stations:
        raise SystemExit("No stations were found for this account")

    by_id = {station.station_id: station for station in stations}
    if requested_station_id:
        station = by_id.get(requested_station_id)
        if station is None:
            available = ", ".join(station.station_id for station in stations)
            raise SystemExit(
                f"Station {requested_station_id!r} was not discovered. Available: {available}"
            )
        return station

    if select_first:
        return stations[0]

    if not sys.stdin.isatty():
        raise SystemExit(
            "Station selection requires a terminal. Pass --station-id or set "
            "SEMS_POWER_STATION_ID."
        )

    print("\nAvailable SEMS stations:")
    for index, station in enumerate(stations, start=1):
        print(f"  {index}. {station.name} ({station.station_id})")

    while True:
        selection = input("Select a station number: ").strip()
        try:
            return stations[int(selection) - 1]
        except (ValueError, IndexError):
            print(f"Enter a number from 1 to {len(stations)}.")


def _write_json(path: Path, value: Any) -> None:
    """Write an analysis artifact as formatted JSON."""
    path.write_text(
        json.dumps(value, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )
    logging.info("Saved %s", path)


def _capture_record(topic: str, payload: bytes) -> dict[str, Any]:
    """Build a lossless record with the normalized station-flow update."""
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
                "normalized_powerflow": None,
            }
        )
        return record

    parsed_payload = decode_mqtt_payload(payload)
    record.update(
        {
            "raw_encoding": "utf-8",
            "raw_payload": raw_text,
            "parsed_payload": parsed_payload,
            "normalized_powerflow": normalize_mqtt_powerflow_payload(parsed_payload),
        }
    )
    return record


async def _capture_mqtt(
    api: SemsApi,
    station: SemsStation,
    output_file: Path,
    duration: float,
) -> int:
    """Capture the selected station's live MQTT feed."""
    if duration <= 0:
        raise ValueError("MQTT capture duration must be greater than zero")

    enabled = await asyncio.to_thread(api.enableSecondData, station.station_id)
    if not enabled:
        raise ValueError("SEMS second-data enable call failed")

    config_data = await asyncio.to_thread(api.getMqttConfig)
    config = SemsMqttConfig.from_api(config_data)
    tls_context = ssl.create_default_context() if config.use_tls else None
    topic = f"/goodwe/second-data/station/{station.station_id}"
    message_count = 0

    logging.info(
        "Connecting to live feed for %s at %s:%d%s",
        station.name,
        config.hostname,
        config.port,
        config.websocket_path,
    )

    with output_file.open("a", encoding="utf-8") as capture:
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
        ) as client:
            await client.subscribe(topic, qos=0)
            logging.info("Subscribed to %s", topic)

            try:
                async with asyncio.timeout(duration):
                    async for message in client.messages:
                        record = _capture_record(
                            str(message.topic), bytes(message.payload)
                        )
                        payload_station_id = (
                            record["normalized_powerflow"].get("station_id")
                            if isinstance(record["normalized_powerflow"], dict)
                            else None
                        )
                        if (
                            isinstance(payload_station_id, str)
                            and payload_station_id != station.station_id
                        ):
                            logging.warning("Ignoring message for another station")
                            continue

                        capture.write(
                            json.dumps(record, ensure_ascii=False, default=str) + "\n"
                        )
                        capture.flush()
                        message_count += 1
                        logging.info(
                            "Live message %d received at %s: %s",
                            message_count,
                            record["received_at"],
                            record["normalized_powerflow"],
                        )
            except TimeoutError:
                pass

    return message_count


async def _async_main(args: argparse.Namespace) -> None:
    """Run station discovery, REST normalization input, and MQTT capture."""
    load_dotenv(REPOSITORY_ROOT / ".env")
    log_dir = REPOSITORY_ROOT / "logs"
    log_dir.mkdir(exist_ok=True)
    timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    _configure_logging(log_dir / f"{timestamp}_local_test.log", args.debug)

    username = os.getenv("SEMS_USERNAME")
    password = os.getenv("SEMS_PASSWORD")
    if not username or not password:
        raise SystemExit("SEMS_USERNAME and SEMS_PASSWORD must be set in .env")

    logging.info("Authenticating SEMS account")
    api = SemsApi(MinimalHass(), username, password, DEFAULT_SEMS_REGION)
    if not await asyncio.to_thread(api.test_authentication):
        raise SystemExit("Authentication failed")

    logging.info("Discovering named stations")
    stations = await asyncio.to_thread(api.get_stations)
    _write_json(
        log_dir / f"{timestamp}_stations.json",
        [_station_record(station) for station in stations],
    )

    requested_station_id = args.station_id or os.getenv("SEMS_POWER_STATION_ID")
    station = _select_station(stations, requested_station_id, args.first_station)
    logging.info("Selected station %s", station.name)

    logging.info("Fetching REST snapshot")
    monitoring_data = await asyncio.to_thread(api.getData, station.station_id)
    if not monitoring_data:
        raise SystemExit("Failed to fetch station monitoring data")
    _write_json(
        log_dir / f"{timestamp}_monitoring_data.json",
        monitoring_data,
    )

    inverter_count = (
        len(monitoring_data["inverter"])
        if isinstance(monitoring_data.get("inverter"), list)
        else 0
    )
    battery_count = sum(
        len(inverter.get("invert_full", {}).get("more_batterys", []))
        for inverter in monitoring_data.get("inverter", [])
        if isinstance(inverter, dict)
        and isinstance(inverter.get("invert_full"), dict)
        and isinstance(inverter["invert_full"].get("more_batterys"), list)
    )
    logging.info(
        "REST snapshot contains %d inverter(s) and %d identified battery record(s)",
        inverter_count,
        battery_count,
    )

    if args.api_only:
        logging.info("API-only validation complete")
        return

    mqtt_file = log_dir / f"{timestamp}_mqtt_messages.jsonl"
    message_count = await _capture_mqtt(
        api,
        station,
        mqtt_file,
        args.duration,
    )
    logging.info(
        "MQTT capture complete: %d message(s) written to %s",
        message_count,
        mqtt_file,
    )


def main() -> None:
    """Run the local flow and handle an interactive stop cleanly."""
    try:
        asyncio.run(_async_main(_parse_arguments()))
    except KeyboardInterrupt:
        logging.info("Capture stopped by user")


if __name__ == "__main__":
    main()
