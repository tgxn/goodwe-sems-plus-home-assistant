# Local SEMS REST and MQTT Capture

Use the local harness to take one REST baseline and then capture the live MQTT
feed used by the SEMS+ website. MQTT is transported over WebSocket; the script
obtains the temporary broker URL and credentials through the authenticated SEMS
API.

The integration currently selects the Australian SEMS region by default.

## Setup

```bash
# create a venv
python3 -m venv venv

# activate it
source venv/bin/activate

# install requirements
pip install -r requirements.local.txt

# create local .env for creds if required
cp .env.example .env
```

Set `SEMS_USERNAME` and `SEMS_PASSWORD` in `.env`. For accounts with multiple
stations, optionally set `SEMS_POWER_STATION_ID`; otherwise the first returned
station is used.

## Capture

Start an open-ended capture and stop it with Ctrl+C after about ten minutes:

```bash
python scripts/test_api_v2.py
```

Messages are printed as they arrive and flushed to disk individually, so a
Ctrl+C stop does not discard prior messages.

## Output

Each run creates timestamped files under `logs/`:

| File                            | Purpose                                   |
| ------------------------------- | ----------------------------------------- |
| `*_local_test.log`              | Connection events and streamed payloads   |
| `*_raw_getPowerStationIds.json` | Raw station discovery response            |
| `*_raw_monitoring_data.json`    | Initial REST baseline                     |
| `*_mqtt_messages.jsonl`         | One lossless MQTT message record per line |

Each MQTT record contains its UTC receive time, topic, exact raw payload, raw
encoding, and parsed payload. UTF-8 JSON is decoded, including the nested
`message` or `msg` JSON used by the website. Non-UTF-8 payloads are retained as
base64 for later protocol analysis.

The local logs and captures intentionally contain unredacted account identifiers,
station IDs, serial numbers, topics, and telemetry for protocol analysis. MQTT
passwords and API tokens are not written. Treat every output file as sensitive.
