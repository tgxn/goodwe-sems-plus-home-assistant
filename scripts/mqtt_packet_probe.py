#!/usr/bin/env python3

import base64
import hashlib
import json
import os
import ssl
import sys
import time
from urllib.parse import urlparse

import paho.mqtt.client as mqtt
import requests
from dotenv import load_dotenv


WAIT_SECONDS = 120

LOGIN_URL = "https://au-semsplus.goodwe.com/web/sems/sems-user/api/v1/auth/cross-login"
API_URL = "https://au.semsportal.com/api"
GATEWAY_URL = "https://au-gateway.semsportal.com/web/sems"
BROKER_URL = "wss://netty-wss-au.iot.goodwe-power.com:8885/mqtt"


def signature(token=None):
    now = round(time.time() * 1000)
    uid = token.get("uid", "") if token else ""
    value = token.get("token", "") if token else ""
    digest = hashlib.sha256(f"{now}@{uid}@{value}".encode()).hexdigest()
    return base64.b64encode(f"{digest}@{now}".encode()).decode()


def headers(token):
    return {
        "Content-Type": "application/json",
        "token": json.dumps(token),
        "X-Signature": signature(token),
    }


def login(username, password):
    print("\nLOGIN")

    password_hash = hashlib.md5(password.encode(), usedforsecurity=False).hexdigest()
    password_hash = base64.b64encode(password_hash.encode()).decode()

    response = requests.post(
        LOGIN_URL,
        headers={
            "Content-Type": "application/json",
            "Token": '{"uid":"","timestamp":0,"token":"","client":"semsPlusWeb","version":"","language":"en"}',
            "X-Signature": signature(),
        },
        json={
            "account": username,
            "pwd": password_hash,
            "agreement": 1,
            "isChinese": False,
            "isLocal": False,
        },
        timeout=30,
    )
    response.raise_for_status()
    data = response.json()

    if data.get("code") not in (0, "0", "00000"):
        sys.exit(f"Login failed: {data.get('msg')}")

    token = data["data"]
    token["api"] = data.get("api") or token.get("api") or GATEWAY_URL
    print("OK")
    print(json.dumps(data, indent=2))

    return token


def get_station_ids(token):
    print("\nSTATIONS")

    response = requests.post(
        f"{API_URL}/PowerStation/GetPowerStationIdByOwner",
        headers=headers(token),
        timeout=30,
    )
    response.raise_for_status()
    data = response.json()["data"]

    if isinstance(data, str):
        station_ids = [data]
    else:
        station_ids = [
            station
            if isinstance(station, str)
            else station.get("id")
            or station.get("powerstation_id")
            or station.get("powerStationId")
            or station.get("stationId")
            for station in data
        ]
        station_ids = [station_id for station_id in station_ids if station_id]

    if not station_ids:
        sys.exit("No stations found")

    print(f"Found {len(station_ids)} station(s)")
    return station_ids


def enable_second_data(token, station_id):
    print("\nENABLE SECOND DATA")

    response = requests.get(
        f"{GATEWAY_URL}/sems-plant/api/second-data/enable",
        params={"stationId": station_id},
        headers=headers(token),
        timeout=30,
    )
    response.raise_for_status()
    print(json.dumps(response.json(), indent=2))


def get_mqtt_config(token):
    print("\nMQTT CONFIG")

    response = requests.get(
        f"{token['api'].rstrip('/')}/sems-plant/api/second-data/config",
        headers=headers(token),
        timeout=30,
    )
    response.raise_for_status()
    data = response.json()

    if data.get("code") not in (0, "0", "00000"):
        sys.exit(f"MQTT config failed: {data.get('msg')}")

    print("OK")
    print(json.dumps(data, indent=2))
    return data["data"]


def make_mqtt_client(mqtt_config, station_id):
    broker = urlparse(mqtt_config.get("brokerUrl") or BROKER_URL)
    topic = f"/goodwe/second-data/station/{station_id}"
    messages = []

    client = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2,
        client_id=mqtt_config["clientId"],
        clean_session=True,
        protocol=mqtt.MQTTv311,
        transport="websockets",
    )
    client.username_pw_set(mqtt_config["userName"], mqtt_config["password"])
    client.tls_set_context(ssl.create_default_context())
    client.ws_set_options(path=broker.path or "/mqtt")
    client.on_message = lambda client, userdata, message: messages.append(message)

    return client, broker, topic, messages


def connect_mqtt(client, broker):
    print("\nMQTT CONNECT")

    client.connect(broker.hostname, broker.port or 443, keepalive=60)
    while not client.is_connected():
        client.loop(timeout=1)

    print("Connected")


def subscribe_and_wait(client, topic, messages):
    print("\nMQTT SUBSCRIBE")

    client.subscribe(topic, qos=0)
    print(topic)
    print(f"Waiting {WAIT_SECONDS} seconds")

    end = time.monotonic() + WAIT_SECONDS
    count = 0

    while time.monotonic() < end:
        client.loop(timeout=1)

        while messages:
            message = messages.pop(0)
            count += 1
            payload = message.payload.decode(errors="replace")

            try:
                payload = json.loads(payload)
                key = "message" if "message" in payload else "msg"
                if isinstance(payload.get(key), str):
                    payload[key] = json.loads(payload[key])
                payload = json.dumps(payload, indent=2)
            except (json.JSONDecodeError, TypeError):
                pass

            print(f"\nMESSAGE {count}\n{payload}")

    return count


def disconnect_mqtt(client):
    print("\nMQTT DISCONNECT")
    client.disconnect()
    client.loop(timeout=1)


def main():
    load_dotenv()

    username = os.getenv("SEMS_USERNAME")
    password = os.getenv("SEMS_PASSWORD")
    station_id = os.getenv("SEMS_POWER_STATION_ID")

    if not username or not password:
        sys.exit("Set SEMS_USERNAME, SEMS_PASSWORD in .env")

    token = login(username, password)
    station_ids = get_station_ids(token)
    station_id = station_id or station_ids[0]
    print(f"Using station: {station_id}")

    enable_second_data(token, station_id)
    mqtt_config = get_mqtt_config(token)
    client, broker, topic, messages = make_mqtt_client(mqtt_config, station_id)

    count = 0
    try:
        connect_mqtt(client, broker)
        count = subscribe_and_wait(client, topic, messages)
    finally:
        disconnect_mqtt(client)

    print(f"\nDone. {count} message(s).")


if __name__ == "__main__":
    main()
