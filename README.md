# GoodWe SEMS+ for Home Assistant

A Home Assistant custom integration for GoodWe systems monitored through
[SEMS+](https://au-semsplus.goodwe.com/). It combines the SEMS+ live feed
(updates every few seconds) with the SEMS+ web APIs for energy totals and
per-device detail.

This integration uses undocumented GoodWe cloud APIs (see [API.md](API.md)).
GoodWe may change them without notice.

## Features

- **Live power**: solar, home load, grid and battery power pushed over the
  SEMS+ live feed (MQTT) every few seconds, with a REST fallback while the feed
  is down.
- **Energy dashboard ready**: daily and month-to-date solar, grid import/export,
  home consumption and battery charge/discharge energy.
- **One device per piece of hardware**: the station (whole system), the
  inverter / all-in-one, each battery rack and the communication dongle.
- **Per-battery detail**: state of charge, health, charge/discharge power and
  energy, and cell voltages/temperatures for every battery rack.
- **Read only**: no inverter or battery controls are exposed.

## Requirements

- Home Assistant 2026.8 or newer.
- A SEMS+ account for the station. A read-only visitor account is recommended.
- Currently supports SEMS+ Australia (`au-semsplus.goodwe.com`).

## Installation

Copy `custom_components/sems_plus` from this repository into the
`custom_components` directory of your Home Assistant configuration, then
restart Home Assistant.

## Configuration

1. Go to **Settings > Devices & services > Add integration** and search for
   **GoodWe SEMS+**.
2. Sign in with your SEMS+ credentials and select the region.
3. Select the station to add.
4. Review the devices that will be added and the update intervals:

   | Setting                   | Default | Purpose                                                      |
   | ------------------------- | ------- | ------------------------------------------------------------ |
   | Poll interval             | 300 s   | Energy totals, device list and per-device values             |
   | Live fallback interval    | 120 s   | Live power polling, only while the live feed is disconnected |
   | Metadata refresh interval | 3600 s  | Firmware versions, capacities and grid connection state      |

Intervals can be changed later from the integration's **Configure** menu. Run
**Add integration** again to add another station.

## Devices and entities

Each entity belongs to the device its data describes. Values that describe the
whole system live on the station. Each piece of hardware reports its own
values on its own device, so the station's "Battery power" and a rack's
"Power" are separate, clearly named entities.

### Station (whole system)

| Group                         | Entities                                                                                                                                                                                                                                                                                  |
| ----------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Live power                    | Solar power, Home load power, Grid power, Grid import power, Grid export power, Battery power, Battery charging power, Battery discharging power, Inverter output power, PV input power, Reactive power, Grid frequency, Power factor, Battery state of charge, Battery state, Grid state |
| Energy (today and this month) | Solar energy, Grid import, Grid export, Home consumption, Solar self-use, Battery charge, Battery discharge                                                                                                                                                                               |
| Daily rates and income        | Self-sufficiency today, Solar self-use rate today, Income today                                                                                                                                                                                                                           |
| Battery system                | Battery system status, Battery state of health, Battery max/min cell temperature, Battery max/min cell voltage                                                                                                                                                                            |
| Station                       | Status, Full load hours today, Inverter / PV / Battery capacity                                                                                                                                                                                                                           |
| Diagnostics                   | MQTT live feed (connected), MQTT live feed status, MQTT connection failures, MQTT messages received, MQTT last message, Last live update                                                                                                                                                  |

The live feed diagnostics show whether the MQTT feed is connected, its state,
how many connection attempts have failed in a row, how many live updates have
been received (a running total, so its long-term statistics show the message
rate over time), when the last live feed message arrived, and when live power
was last updated from either the live feed or the REST fallback.

Sign convention: **Grid power** is positive when importing and negative when
exporting. **Battery power** is positive when discharging and negative when
charging. The separate import/export and charging/discharging sensors are
always positive.

### Inverter / all-in-one

Status, AC power, PV input power, backup power/voltage/current, grid
voltage/current/frequency, reactive power, per-string PV power/voltage/current,
temperature, working hours, PV energy and battery charge/discharge energy
(today, this month, total), and an **On grid** binary sensor.

### Battery rack (one device per rack)

Status, state of charge, state of health, power (signed like the station),
charging power, discharging power, voltage, max charge/discharge power,
max/min cell temperature, max/min cell voltage, and charge/discharge energy
(today, this month, total).

### Dongle

Status, communication mode and signal strength.

Any new numeric value reported by SEMS+ that the integration does not know yet
is added as a disabled entity, so it can be enabled and inspected.

## Energy dashboard

| Energy dashboard setting  | Entity                              |
| ------------------------- | ----------------------------------- |
| Grid consumption          | Station **Grid import today**       |
| Return to grid            | Station **Grid export today**       |
| Solar production          | Station **Solar energy today**      |
| Battery: energy going in  | Station **Battery charge today**    |
| Battery: energy going out | Station **Battery discharge today** |
| Grid power                | Station **Grid power**              |
| Battery power             | Station **Battery power**           |

The daily counters reset at midnight (station time). Home Assistant handles
this automatically for `total_increasing` sensors.

## How updates work

- **Live feed**: after setup the integration enables SEMS+ "second data" for
  the station and subscribes to its MQTT feed. Live power arrives every few
  seconds and is re-enabled periodically so the feed keeps flowing.
- **Live fallback**: while the live feed is not connected, live power is polled
  from the REST API at the live fallback interval.
- **Poll**: energy totals, the device list and per-device values are fetched at
  the poll interval.
- **Metadata**: firmware versions, capacities and the grid connection state
  are fetched at the metadata refresh interval.

## Status values

SEMS+ does not document its status codes. The labels below are inferred from
observed data. Any other code shows as `unknown`, and the raw code is available
in the entity's `code` attribute.

| Entity                         | Code → state                              |
| ------------------------------ | ----------------------------------------- |
| Station status                 | 1 working, 0 waiting, 2 fault, -1 offline |
| Inverter / battery rack status | 5 running                                 |
| Dongle status                  | 1 online                                  |
| Battery system status          | 7 discharging                             |

## Troubleshooting

Enable debug logging from the integration page, reproduce the problem, then
disable debug logging to download the log. Or add to `configuration.yaml`:

```yaml
logger:
  default: info
  logs:
    custom_components.sems_plus: debug
```

Logs **should** redact credentials, tokens, station IDs and serial numbers. When sharing
logs or API captures, check for and remove anything personal.

## Development

```bash
python -m pip install -r requirements.test.txt
python -m pytest tests -v
ruff check custom_components tests
ruff format --check custom_components tests
mypy custom_components --python-version 3.14
```

`scripts/test_api_v2.py` signs in with `SEMS_USERNAME` / `SEMS_PASSWORD` from
`.env`, calls every API method for the first station, captures the live feed
for 60 seconds, and writes each response to `logs/<timestamp>_api_v2/`. It's
useful for checking new hardware or API changes. The API is documented in
[API.md](API.md).

## Credits

This integration is based on
[TimSoethout/goodwe-sems-home-assistant](https://github.com/TimSoethout/goodwe-sems-home-assistant),
which was inspired by
[Sprk-nl/goodwe_sems_portal_scraper](https://github.com/Sprk-nl/goodwe_sems_portal_scraper)
and [bouwew/sems2mqtt](https://github.com/bouwew/sems2mqtt).

This project is not affiliated with or endorsed by GoodWe.
