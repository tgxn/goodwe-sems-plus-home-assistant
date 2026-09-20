#!/usr/bin/env python3
"""
Dedicated MQTT SUBSCRIBE test to debug
Tests the exact flow: CONNECT → CONNACK → SUBSCRIBE → SUBACK → wait for PUBLISH

Uses known values from PAYLOADS.md:
- Broker: wss://netty-wss-au.iot.goodwe-power.com:8885/mqtt
- Client ID: SEMS_PLUS_WebClient_* (dynamic from getMqttConfig)
"""

import asyncio
import json
import logging
import os
import ssl
import sys
from datetime import datetime
from pathlib import Path

import aiomqtt
from dotenv import load_dotenv

# Import SEMS components
sys.path.insert(0, str(Path(__file__).parent.parent))
from custom_components.sems_au.sems_api import SemsApi


# Setup minimal Home Assistant mock
class MinimalHass:
    def async_add_executor_job(self, func, *args):
        loop = asyncio.get_event_loop()
        return loop.run_in_executor(None, func, *args)


# Configure logging
logging.basicConfig(
    level=logging.DEBUG, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# Load environment
load_dotenv()
username = os.getenv("SEMS_USERNAME")
password = os.getenv("SEMS_PASSWORD")

if not username or not password:
    logger.error("SEMS_USERNAME and SEMS_PASSWORD must be set in .env")
    sys.exit(1)


async def test_mqtt_subscribe():
    """Test MQTT SUBSCRIBE with known values."""
    logger.info("MQTT SUBSCRIBE TEST")

    # Step 1: Authenticate and get MQTT config
    logger.info("\n[STEP 1] Authenticating SEMS account...")
    hass = MinimalHass()
    api = SemsApi(hass, username, password)

    # Step 2: Get power stations
    logger.info("\n[STEP 1] Fetching power stations...")
    try:
        power_stations = await asyncio.to_thread(api.getPowerStationIds)
        logger.debug(
            f"Power stations response type: {type(power_stations)}, content: {power_stations}"
        )

        if isinstance(power_stations, str):
            # Response might be a station ID string directly
            station_id = power_stations
            logger.info(f"✓ Using station: {station_id}")
        elif isinstance(power_stations, list) and len(power_stations) > 0:
            station = power_stations[0]
            if isinstance(station, dict):
                station_id = station.get("id")
            else:
                station_id = station
            logger.info(f"✓ Using station: {station_id}")
        else:
            logger.error(f"✗ Unexpected power stations format: {power_stations}")
            return
    except Exception as e:
        logger.error(f"✗ Failed to get power stations: {e}", exc_info=True)
        return

    # wait 1 second before proceeding to the next step
    await asyncio.sleep(1)

    try:
        mqtt_config = await asyncio.to_thread(api.getMqttConfig)
        logger.info("✓ MQTT config retrieved")
        logger.debug(f"  clientId: {mqtt_config.get('clientId')}")
        logger.debug(f"  userName: {mqtt_config.get('userName')[:20]}...")
        logger.debug(f"  password: {mqtt_config.get('password')[:20]}...")
    except Exception as e:
        logger.error(f"✗ Failed to get MQTT config: {e}")
        return

    # wait 1 second before proceeding to the next step
    await asyncio.sleep(1)

    # Step 3: Test raw MQTT connection with aiomqtt
    logger.info("\n[STEP 3] Connecting to MQTT broker...")
    logger.info(f"  Broker: netty-wss-au.iot.goodwe-power.com:8885/mqtt")
    logger.info(f"  Topic: /goodwe/second-data/station/{station_id}")

    try:
        # Build connection parameters exactly as in sems_mqtt.py
        hostname = "netty-wss-au.iot.goodwe-power.com"
        port = 8885
        websocket_path = "/mqtt"

        client_id = mqtt_config.get("clientId")
        username_mqtt = mqtt_config.get("userName")
        password_mqtt = mqtt_config.get("password")

        logger.info("\n[STEP 3a] CONNECT phase...")
        logger.debug(f"  client_id: {client_id}")
        logger.debug(f"  username: {username_mqtt[:30]}...")
        logger.debug(f"  password: {password_mqtt[:30]}...")

        # Create TLS context
        tls_context = await asyncio.to_thread(ssl.create_default_context)

        # Connect to broker
        async with aiomqtt.Client(
            hostname=hostname,
            port=port,
            identifier=client_id,
            username=username_mqtt,
            password=password_mqtt,
            protocol=aiomqtt.ProtocolVersion.V311,
            transport="websockets",
            tls_context=tls_context,
            websocket_path=websocket_path,
            keepalive=60,
            will=None,
        ) as client:
            logger.info("✓ CONNECT/CONNACK successful")

            # Subscribe to station topic
            topic = f"/goodwe/second-data/station/{station_id}"
            logger.info(f"\n[STEP 3b] SUBSCRIBE phase...")
            logger.debug(f"  topic: {topic}")

            await client.subscribe(topic, qos=0)
            logger.info("✓ SUBSCRIBE/SUBACK successful")

            # Wait for messages
            logger.info(f"\n[STEP 3c] Waiting for PUBLISH messages...")
            logger.info("(timeout: 30 seconds)\n")

            message_count = 0
            start_time = datetime.now()
            timeout = 30

            try:
                async for message in client.messages:
                    message_count += 1
                    elapsed = (datetime.now() - start_time).total_seconds()

                    logger.info(f"✓ [+{elapsed:.1f}s] Message #{message_count}:")
                    logger.info(f"  Topic: {message.topic}")
                    logger.info(f"  Payload length: {len(message.payload)} bytes")

                    try:
                        payload = json.loads(message.payload.decode())
                        logger.info(
                            f"  Payload (parsed): {json.dumps(payload, indent=4)}"
                        )
                    except Exception:
                        logger.info(f"  Payload (raw): {message.payload[:100]}")

                    if elapsed > timeout:
                        logger.info(f"\nTimeout reached ({timeout}s), stopping")
                        break

            except TimeoutError:
                logger.warning(f"✗ No messages received within {timeout} seconds")

            if message_count == 0:
                logger.warning("✗ No PUBLISH messages received")
            else:
                logger.info(f"\n✓ SUCCESS: Received {message_count} message(s)")

    except Exception as e:
        logger.error(f"✗ MQTT connection failed: {e}", exc_info=True)
        return


async def main():
    """Run the test."""
    try:
        await test_mqtt_subscribe()
    except KeyboardInterrupt:
        logger.info("\nTest interrupted by user")
    except Exception as e:
        logger.error(f"Test failed: {e}", exc_info=True)


if __name__ == "__main__":
    asyncio.run(main())
