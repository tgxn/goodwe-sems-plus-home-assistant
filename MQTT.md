# SEMS MQTT / WebSocket Notes

This is the working model for the live SEMS feed used by the website and now mirrored in the integration. The important point is that this is MQTT over WebSockets, not a bespoke socket protocol. The WebSocket handshake is just the transport; the MQTT session contains the actual live station messages.

## 1. Auth and config flow

The live stream is established from the same authenticated SEMS session used by the rest of the integration.

The order is:

1. Authenticate with the SEMS web login flow.
2. Fetch the station list and choose the active station ID.
3. Enable second-data (live updates) for the station:
   - GET /sems-plant/api/second-data/enable?stationId={station_id}
4. Call the MQTT config endpoint:
   - GET /sems-plant/api/second-data/config
5. Read the short-lived MQTT credentials from the response:
   - clientId
   - userName
   - password
6. Open an MQTT-over-WebSockets client and subscribe to the station topic.

This branch is intentionally AU-first and keeps the config model minimal: the integration owns a region map with AU as the default, and it does not carry dead country-override logic or alternate broker endpoints in the config flow.

The critical detail is that this config endpoint is not a custom binary protocol. It is simply the broker/session metadata the website uses to create a standard MQTT client for a real-time station channel.

## 2. Broker and transport details

The AU broker endpoint is:

- wss://netty-wss-au.iot.goodwe-power.com:8885/mqtt

The transport is standard MQTT over WebSockets:

- websocket handshake target: /mqtt
- protocol: MQTT 3.1.1
- transport: websockets
- Origin: https://au-semsplus.goodwe.com
- TLS: enabled for the AU broker

The raw Sec-WebSocket-Key in the WebSocket handshake is not a secret and is not a reusable token. It is just a transport-level handshake value. The actual auth is the MQTT username/password returned by the config endpoint.

## 3. MQTT connection parameters

The integration builds the MQTT client with the same values the website does:

- hostname: broker host from the region config
- port: 8885 for AU
- websocket_path: /mqtt
- username: config["userName"]
- password: config["password"]
- identifier: config["clientId"]
- protocol: MQTT 3.1.1

The config response is always short-lived and is expected to rotate or expire. The connection should therefore be created fresh on startup and on reconnect, rather than cached forever.

## 4. Topic and payload shape

The live station topic is:

- /goodwe/second-data/station/{station_id}

The payload is typically UTF-8 JSON. The important pattern is that the real station payload is often wrapped in another JSON object. The most common shape is:

```json
{
  "title": "Live data",
  "message": "{\"sn\":\"GW123456789012\",\"power\":42}"
}
```

or:

```json
{
  "code": 0,
  "msg": "{\"stationId\":\"abcde\",\"data\":{...}}"
}
```

The decoder unwraps the nested JSON by doing this:

1. parse the outer JSON
2. look for either message or msg
3. if the value is a string, parse it as JSON again
4. replace the nested field with the parsed object

This produces a normalized object such as:

```json
{
  "title": "Live data",
  "message": {
    "sn": "GW123456789012",
    "power": 42
  }
}
```

The reason this matters is that the JavaScript app expects a real object graph after decoding, not a flat string blob. All downstream processing should work with the unwrapped payload, not the raw nested string.

The current Home Assistant normalization maps live MQTT fields as follows:

| MQTT field | Coordinator field                        | Unit exposed in HA |
| ---------- | ---------------------------------------- | ------------------ |
| `pSystem`  | `powerflow.pv`, `powerflow.system_power` | W                  |
| `pConsum`  | `powerflow.load`                         | W                  |
| `pGrid`    | `powerflow.grid`                         | W                  |
| `pBat`     | `powerflow.battery`                      | W                  |
| `pAc`      | `powerflow.ac_power`                     | W                  |
| `pDc`      | `powerflow.dc_power`                     | W                  |
| `qAc`      | `powerflow.reactive_power`               | var                |
| `fAc`      | `powerflow.grid_frequency`               | Hz                 |
| `pf`       | `powerflow.power_factor`                 | ratio              |
| `soc`      | `powerflow.soc`                          | %                  |

`stationId`, `time`, `traceId`, and `flows` are retained with the normalized update for source/debug context. They are not exposed as measurement sensors.

## 5. Example auth/config/reconnect sequence

A full connection sequence looks like this:

1. POST auth, receive a session token.
2. GET /PowerStation/GetPowerStationIdByOwner and resolve station IDs.
3. For each station, optionally call the main GET /v3/PowerStation/GetMonitorDetailByPowerstationId endpoint to fetch station metadata.
4. GET /sems-plant/api/second-data/enable?stationId={station_id} to activate live updates for the station.
5. GET /sems-plant/api/second-data/config to obtain the short-lived MQTT session.
6. Create aiomqtt.Client with the broker URL, MQTT credentials, and WebSocket Origin header.
7. Subscribe to /goodwe/second-data/station/{station_id}.
8. Read client.messages in a loop.
9. On disconnect, auth failure, or any unexpected MQTT error, wait for a bounded backoff and retry.

The connection loop is meant to survive temporary auth/session expiry and socket churn without restarting Home Assistant.

## 6. Reconnect and backoff behavior

The listener keeps a failure counter and increases the retry delay exponentially until it reaches a cap, for example:

- attempt 1: 5 seconds
- attempt 2: 10 seconds
- attempt 3: 20 seconds
- attempt 4: 40 seconds
- capped: 60 seconds

This is intentionally conservative because the broker/session may reject bad MQTT credentials or require a fresh config fetch after some auth or network churn.

The reconnect loop should:

- recreate the MQTT client whenever the session expires
- re-fetch the config rather than reusing stale credentials
- back off after repeated failures to avoid hammering the broker
- stop cleanly if Home Assistant unloads the entry

## 7. Multiple stations and station-aware naming

The integration can support multiple power stations by creating separate config entries per station ID. This is the correct HA model because each station has its own live MQTT subscription and its own coordinator data.

The station-scoped naming should be implemented as:

- config entry title: {station_name}: {station_id} or Station {station_id}
- device title: {station_name}
- entity unique IDs: {station_id}-{inverter_serial}-{field} for inverter data
- entity unique IDs: {station_id}-powerflow-{field} for station powerflow data

This avoids collisions when multiple stations are present and keeps the HA entity tree readable in a multi-site setup.

## 8. Logging and safety

The integration keeps production logs sanitized. It does not emit raw tokens, email addresses, or long serials in free-form debug output. The MQTT payload itself may be logged only after decoding and redaction, and sensitive keys are filtered before being written.

The WebSocket handshake key is random and not a secret. It should not be treated as an auth token or persisted as a credential.
