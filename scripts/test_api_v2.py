#!/usr/bin/env python3
"""Call every SEMS+ v2 API method and save each response under logs/."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import os
import re
import sys
from collections.abc import Awaitable
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import aiohttp
from dotenv import load_dotenv

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPOSITORY_ROOT))

from custom_components.sems_plus.const import (  # noqa: E402
    DEFAULT_SEMS_REGION,
    SEMS_REGIONS,
    redact_for_log,
)
from custom_components.sems_plus.sems_api_v2 import (  # noqa: E402
    SemsLiveData,
    SemsPlusClient,
)

LOG_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
MQTT_CAPTURE_SECONDS = 60


class StepRunner:
    """Run API calls in order, writing each result to a numbered JSON file."""

    def __init__(self, output_dir: Path) -> None:
        """Initialize the runner."""
        self.output_dir = output_dir
        self.step = 0

    def write_json(self, name: str, value: Any) -> None:
        """Write one result as formatted JSON."""
        self.step += 1
        safe_name = re.sub(r"[^A-Za-z0-9_.-]", "_", name)
        path = self.output_dir / f"{self.step:02d}_{safe_name}.json"
        path.write_text(
            json.dumps(value, indent=2, ensure_ascii=False, default=str),
            encoding="utf-8",
        )
        logging.info("Saved %s", path.name)

    async def run(self, name: str, call: Awaitable[Any], redact: bool = False) -> Any:
        """Await one API call, save its result, and keep going on failure."""
        logging.info("Calling %s", name)
        try:
            result = await call
        except Exception as err:
            logging.exception("%s failed", name)
            self.write_json(name, {"error": f"{type(err).__name__}: {err}"})
            return None
        self.write_json(name, redact_for_log(result) if redact else result)
        return result


def configure_logging(log_file: Path) -> None:
    """Configure matching console and file logs."""
    logging.basicConfig(
        level=logging.DEBUG,
        format=LOG_FORMAT,
        handlers=[logging.FileHandler(log_file), logging.StreamHandler()],
        force=True,
    )


async def capture_mqtt(
    client: SemsPlusClient, station_id: str, output_file: Path
) -> int:
    """Capture the station's live feed for a fixed period."""
    message_count = 0

    with output_file.open("a", encoding="utf-8") as capture:

        def on_update(live_data: SemsLiveData) -> None:
            nonlocal message_count
            message_count += 1
            capture.write(json.dumps(dataclasses.asdict(live_data), default=str) + "\n")
            capture.flush()
            logging.info(
                "Live message %d: %s", message_count, redact_for_log(live_data.values)
            )

        def on_status(state: str) -> None:
            logging.info("Live feed state: %s", state)

        client.start_mqtt(station_id, on_update, on_status)
        await asyncio.sleep(MQTT_CAPTURE_SECONDS)
        await client.stop_mqtt()

    return message_count


async def async_main() -> None:
    """Exercise every API method against the first station on the account."""
    load_dotenv(REPOSITORY_ROOT / ".env")
    timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    output_dir = REPOSITORY_ROOT / "logs" / f"{timestamp}_api_v2"
    output_dir.mkdir(parents=True, exist_ok=True)
    configure_logging(output_dir / "run.log")

    username = os.getenv("SEMS_USERNAME")
    password = os.getenv("SEMS_PASSWORD")
    if not username or not password:
        raise SystemExit("SEMS_USERNAME and SEMS_PASSWORD must be set in .env")

    runner = StepRunner(output_dir)
    async with aiohttp.ClientSession() as session:
        client = SemsPlusClient(
            session, username, password, SEMS_REGIONS[DEFAULT_SEMS_REGION]
        )

        if await runner.run("login", client.login(), redact=True) is None:
            raise SystemExit("Authentication failed")
        await runner.run("version", client.get_version())

        stations = await runner.run("stations", client.get_stations())
        if not stations:
            raise SystemExit("No stations were found for this account")
        station_id = stations[0]["id"]
        logging.info("Using first station: %s", stations[0].get("name"))

        today = date.today()
        await runner.run("station_flow", client.get_station_flow(station_id))
        await runner.run(
            "station_statistics_today",
            client.get_station_statistics(station_id, today, today),
        )
        await runner.run(
            "station_statistics_month",
            client.get_station_statistics(station_id, today.replace(day=1), today),
        )
        await runner.run(
            "station_production", client.get_station_production(station_id, today)
        )
        await runner.run(
            "station_basic_info", client.get_station_basic_info(station_id)
        )
        devices = await runner.run(
            "station_devices", client.get_station_devices(station_id)
        )

        for device in devices or []:
            sn = device.get("sn")
            device_type = device.get("deviceType")
            if not isinstance(sn, str) or not isinstance(device_type, str):
                continue
            logging.info("Device %s (%s)", device.get("name"), device_type)
            prefix = f"device_{sn}"
            await runner.run(
                f"{prefix}_information",
                client.get_device_information(station_id, sn, device_type),
            )
            await runner.run(
                f"{prefix}_related",
                client.get_device_related(station_id, sn, device_type),
            )
            await runner.run(
                f"{prefix}_telecounting",
                client.get_device_telecounting(station_id, sn, device_type),
            )
            await runner.run(
                f"{prefix}_telemetry",
                client.get_device_telemetry(station_id, sn, device_type),
            )

        await runner.run("enable_second_data", client.enable_second_data(station_id))
        await runner.run("mqtt_config", client.get_mqtt_config(), redact=True)

        logging.info("Capturing live feed for %d seconds", MQTT_CAPTURE_SECONDS)
        message_count = await capture_mqtt(
            client, station_id, output_dir / "mqtt_messages.jsonl"
        )
        logging.info("Live feed capture complete: %d message(s)", message_count)

    logging.info("All output written to %s", output_dir)


def main() -> None:
    """Run the API exerciser and handle an interactive stop cleanly."""
    try:
        asyncio.run(async_main())
    except KeyboardInterrupt:
        logging.info("Stopped by user")


if __name__ == "__main__":
    main()
