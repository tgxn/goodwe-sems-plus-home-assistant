# SEMS+ API Reference

The GoodWe SEMS+ calls used by this integration, captured from the SEMS+
Australia web app. The API is undocumented and may change. IDs and serials
below are placeholders, and long responses are trimmed with `...`.

Hosts (AU):

| Purpose                         | Base URL                                            |
| ------------------------------- | --------------------------------------------------- |
| Login and web app               | `https://au-semsplus.goodwe.com`                    |
| API gateway (`{gateway}`)       | `https://au-gateway.semsportal.com/web/sems`        |
| Live feed (MQTT over WebSocket) | `wss://netty-wss-au.iot.goodwe-power.com:8885/mqtt` |

Every gateway response uses the same envelope. `code` is `"00000"` on success;
`GY0429` means rate limited. Session-expired codes seen so far are `100002` and
`C0602` (账号登录异常). The integration retries any failed call once after a
fresh login, and backs off from logging in again (1 min, doubling up to 30 min)
if the retry still fails.

```json
{ "code": "00000", "description": "成功", "traceId": "...", "data": ... }
```

---

## Authentication

### POST /web/sems/sems-user/api/v1/auth/cross-login

**Endpoint:** `https://au-semsplus.goodwe.com/web/sems/sems-user/api/v1/auth/cross-login`

**Headers:** `Token: {"uid":"","timestamp":0,"token":"","client":"semsPlusWeb","version":"","language":"en"}` and `X-Signature` (below).

**Request Body:**

```json
{
  "account": "user@example.com",
  "pwd": "<base64(md5_hex(password))>", // base64 of the MD5 hex digest of the password
  "agreement": 1,
  "isLocal": false,
  "isChinese": false
}
```

**Response Body:**

```json
{
  "code": "00000",
  "data": {
    "uid": "00000000-0000-4000-8000-00000000000a",
    "timestamp": "1767225600000",
    "token": "<session token>",
    "client": "semsPlusWeb",
    "version": "",
    "language": "en",
    "api": "https://au-gateway.semsportal.com/web/sems",
    "region": "au",
    "uuid": "..."
  }
}
```

**Authenticated requests** send:

- `token`: the whole `data` object above, JSON-encoded.
- `X-Signature`: `base64("{sha256(f'{ms}@{uid}@{token}')}@{ms}")`, where `ms` is
  the current epoch time in milliseconds.

---

## Stations

### POST /sems-plant/api/portal/stations/page

Lists the account's stations (paged). This is the station picker in the config flow.

**Endpoint:** `{gateway}/sems-plant/api/portal/stations/page`

**Request Body:** `{ "current": 1, "size": 100 }`

**Response Body:**

```json
{
  "dataList": [
    {
      "id": "00000000-0000-4000-8000-000000000001",
      "name": "Test Station",
      "status": 1,
      "installedPower": 5.0,
      "pvInstallP": 6.0,
      "productionToday": 10.0,
      "proCharStatsToday": 4.0,
      "proDischarStatsToday": 3.0,
      "productionTotal": 1000.0,
      "proCharStatsTotal": 400.0,
      "proDischarStatsTotal": 300.0,
      "pSystem": 0,
      "fullHourToday": 4.0,
      "specificYield": 1.0,
      "timeZone": "UTC+00:00"
    }
  ],
  "size": 100,
  "current": 1,
  "total": 1
}
```

### GET /sems-plant/api/stations/flow

Current power flow. Power is in **kW** and unsigned; `flows` gives the direction.

**Endpoint:** `{gateway}/sems-plant/api/stations/flow`

**Query Parameters:** `stationId={stationId}`

**Response Body:**

```json
{
  "id": "00000000-0000-4000-8000-000000000001",
  "status": "1",
  "flows": { "pBat": ["pConsum"] },
  "pSystem": 0,
  "pAc": 0.5,
  "pBat": 0.5,
  "pGrid": 0,
  "soc": 50,
  "pConsum": 0.5,
  "refreshTime": "2026-09-23T10:00:00"
}
```

### POST /sems-plant/api/stations/statistics

Energy per day (kWh) for a date range. Top-level totals cover the whole range.

**Endpoint:** `{gateway}/sems-plant/api/stations/statistics`

**Request Body:**

```json
{
  "stationId": "00000000-0000-4000-8000-000000000001",
  "isReport": false,
  "items": [
    "proSystemTotalStats",
    "proConsumStats",
    "proGridStats",
    "proPurchaseStats",
    "proSelfConsumStats",
    "proCharStats",
    "proDischarStats"
  ],
  "dimension": "day",
  "startTime": "2026-09-01 00:00:00",
  "endTime": "2026-09-23 23:59:59"
}
```

**Response Body:**

```json
{
  "proSelfConsum": 7.0, "proSelfConsumRate": 70.0,
  "proGrid": 3.0, "proPurchase": 1.0, "proConsum": 8.0,
  "contributionRate": 87.5,
  "dataList": [
    { "item": "proPurchaseStats", "unit": "kWh",
      "statisticsList": [{ "date": "2026-09-23", "val": 1.0 }] },
    ...
  ]
}
```

| Item                               | Meaning                    |
| ---------------------------------- | -------------------------- |
| `proSystemTotalStats`              | Solar production           |
| `proConsumStats`                   | Home consumption           |
| `proPurchaseStats`                 | Grid import                |
| `proGridStats`                     | Grid export                |
| `proSelfConsumStats`               | Solar used on site         |
| `proCharStats` / `proDischarStats` | Battery charge / discharge |

### POST /sems-plant/api/stations/production

One day's production with income.

**Endpoint:** `{gateway}/sems-plant/api/stations/production`

**Request Body:**

```json
{
  "stationId": "00000000-0000-4000-8000-000000000001",
  "items": [
    "proConsumStats",
    "proGridStats",
    "proPurchaseStats",
    "profitGridStats",
    "profitProStats",
    "proSystemTotalStats"
  ],
  "dimension": "day",
  "isReport": false,
  "startTime": "2026-09-23 00:00:00",
  "endTime": "2026-09-23 23:59:59"
}
```

**Response Body:**

```json
{
  "proSystemTotalStats": 10.0,
  "proGridStats": 3.0,
  "proPurchaseStats": 1.0,
  "proConsumStats": 8.0,
  "profitProStats": 1.5,
  "profitGridStats": 0.5,
  "currency": "AUD"
}
```

### POST /sems-plant/api/portal/stations/basic/info

Station metadata.

**Endpoint:** `{gateway}/sems-plant/api/portal/stations/basic/info`

**Query Parameters:** `stationId={stationId}`

**Request Body:** `{}`

**Response Body:**

```json
{
  "stationId": "00000000-0000-4000-8000-000000000001",
  "name": "Test Station",
  "status": "1", "gridStatus": "1",
  "installedPower": 5.0, "pvCapacity": 6.0, "batteryCapacity": 16.0,
  "zoneId": "UTC", "timeZone": "UTC+00:00",
  "isAllInOne": true, "hasGoodweInverter": true,
  ...
}
```

---

## Devices

### POST /sems-plant/api/web/device/station/page

All devices in a station (paged), with model and status.

**Endpoint:** `{gateway}/sems-plant/api/web/device/station/page`

**Request Body:** `{ "stationId": "{stationId}", "current": 1, "size": 100 }`

**Response Body:**

```json
{
  "dataList": [
    {
      "sn": "INV0000000000001",
      "name": "All-in-One 1",
      "deviceType": "ENERGY_STORAGE_INTEGRATED_CABINET",
      "model": "<inverter model>",
      "status": 5,
      "subtype": "RESIDENTIAL",
      "zoneId": "UTC",
      "brand": "GOODWE"
    },
    {
      "sn": "BAT0000000000001",
      "name": "Battery Rack 1",
      "deviceType": "BATTERY_RACK",
      "model": "<battery model>",
      "status": 5
    },
    {
      "sn": "DNG0000000000001",
      "name": "Dongle 1",
      "deviceType": "DONGLE",
      "status": 1,
      "communicationMode": "LAN",
      "wirelessSignalStrength": "0"
    }
  ],
  "size": 100,
  "current": 1,
  "total": 3
}
```

### Factor shape

`telemetry`, `telecounting` and `information` return **factors**:

```json
{
  "code": "Vac",
  "data": "240",
  "dataType": "NUMERIC",
  "unit": "V",
  "alias": "single_phase_volt"
}
```

`data` is always a string. `telemetry` and `telecounting` group factors as
`[{ "code": "ac", "alias": "ac_parameters", "factors": [...] }]`, while
`information` returns a flat factor list. Units seen: `kW`, `kWh`, `kVar`, `V`,
`mV`, `A`, `Hz`, `℃`, `%`, `kΩ`, `H`.

All four endpoints below take the query parameters
`deviceType={deviceType}&pwId={stationId}`.

### GET /sems-plant/api/equipments/{sn}/telemetry

Live device measurements.

**Endpoint:** `{gateway}/sems-plant/api/equipments/{sn}/telemetry`

| Device type                         | Groups → factor codes                                                                                                                                                                             |
| ----------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `ENERGY_STORAGE_INTEGRATED_CABINET` | `system`: `Temperature`, `hTotal` · `ac`: `pAc`, `qAc`, `Vac`, `Iac`, `Fac` · `backup`: `pBackup`, `vBackupPhA`, `aBackupPhA` · `pv`: `pDc`, `ISOLimit`, `MPPT-n:Ppv`, `MPPT-n:Vpv`, `MPPT-n:Ipv` |
| `BATTERY_RACK`                      | `system`: `serCellTotal`, `version` (BMS), `dcdcVersion` · `runtime`: `soc`, `soh`, `pBat`, `voltage`, `pMaxChar`, `pMaxDischar`, `tempMaxCell`, `tempMinCell`, `vMaxCell`, `vMinCell`            |
| `DONGLE`                            | none (empty list)                                                                                                                                                                                 |

### GET /sems-plant/api/equipments/{sn}/telecounting

Energy counters per period.

**Endpoint:** `{gateway}/sems-plant/api/equipments/{sn}/telecounting`

Groups: `telecounting_real` (`pAc`, `ratedPower` / `soc`, `pBat`) and
`telecounting_today | _week | _month | _year | _lifetime`, holding
`proPvStats{Today|Week|Month|Year|Total}` (inverter only),
`proCharStats…` and `proDischarStats…` in kWh. Dongles return no `data`.

### GET /sems-plant/api/equipments/{sn}/information

Device details: `deviceName`, `modelType`, `status`, `addTime`, plus
type-specific codes:

| Device type                         | Codes                                                                             |
| ----------------------------------- | --------------------------------------------------------------------------------- |
| `ENERGY_STORAGE_INTEGRATED_CABINET` | `safetyVersion` (firmware), `ratedPower`, `gridConnStu` (1 = on grid), `bat1MRSn` |
| `DONGLE`                            | `commModuleVer` (firmware), `communicationMode`, `wirelessSignalStrength`         |

### GET /sems-plant/api/equipments/{sn}/relatedDevices

**Endpoint:** `{gateway}/sems-plant/api/equipments/{sn}/relatedDevices`

**Query Parameters:** `sn={sn}&deviceType={deviceType}&pwId={stationId}`

**Response Body (inverter):** the battery system it manages.

```json
[
  {
    "sn": "VD2001INV0000000000001",
    "no": "1",
    "name": "BAT1",
    "type": "BAT_SYS",
    "status": 7,
    "soc": 50.0,
    "isConnected": true,
    "pbat": 0.5
  }
]
```

---

## Live data

### GET /sems-plant/api/second-data/enable

Turns on the live feed for a station. The web app calls this repeatedly while
a station is open. The integration calls it once per MQTT connection and again
only if no station message arrives for 60 seconds.

**Endpoint:** `{gateway}/sems-plant/api/second-data/enable`

**Query Parameters:** `stationId={stationId}`

**Response Body:** `{ "code": "00000", "data": true }`

### GET /sems-plant/api/second-data/config

Short-lived MQTT credentials. Fetch fresh ones for every connection.

**Endpoint:** `{gateway}/sems-plant/api/second-data/config`

**Response Body:**

```json
{
  "clientId": "SEMS_PLUS_WebClient_...",
  "userName": "<mqtt username>",
  "password": "<mqtt password>"
}
```

### MQTT live feed

**Broker:** `wss://netty-wss-au.iot.goodwe-power.com:8885/mqtt` (MQTT 3.1.1
over WebSocket, TLS). Use `clientId` / `userName` / `password` from the config call.

**Topic:** `/goodwe/second-data/station/{stationId}`

**Payload** (about every 5 seconds; numbers are strings, power in kW):

```json
{
  "stationId": "00000000-0000-4000-8000-000000000001",
  "time": "2026-09-23 10:00:00",
  "traceId": "...",
  "pSystem": "0.0",
  "pConsum": "0.5",
  "pGrid": "0.0",
  "pBat": "0.5",
  "pAc": "0.5",
  "pDc": "0.0",
  "qAc": "0.0",
  "fAc": "50.0",
  "pf": "1.0",
  "soc": "50.0",
  "flows": { "pBat": ["pConsum"] }
}
```

Some payloads may be wrapped as `{ "message": "<json string>" }` or
`{ "msg": "<json string>" }`. Decode the inner string.

---

## Codes and conventions

**Power codes** (flow, MQTT and telemetry): `pSystem` solar, `pConsum` home
load, `pGrid` grid, `pBat` battery, `pAc` inverter AC output, `pDc` PV DC
input, `qAc` reactive power, `fAc` grid frequency, `pf` power factor, `soc`
battery state of charge.

**Direction.** Power values are magnitudes. `flows` maps each source to the
nodes it feeds:

| Flow edge   | Meaning                 |
| ----------- | ----------------------- |
| `pBat → *`  | Battery discharging     |
| `* → pBat`  | Battery charging        |
| `pGrid → *` | Importing from the grid |
| `* → pGrid` | Exporting to the grid   |

**Status codes.** These are undocumented. The values below are inferred from
observed data, and `portal/stations/count` buckets stations as working /
waiting / fault / offline / building.

| Where                            | Code → meaning                                      |
| -------------------------------- | --------------------------------------------------- |
| Station `status`                 | 1 working (0 waiting, 2 fault, -1 offline inferred) |
| Inverter / battery rack `status` | 5 running                                           |
| Dongle `status`                  | 1 online                                            |
| `BAT_SYS` `status`               | 7 discharging                                       |
