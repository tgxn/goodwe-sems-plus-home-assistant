# GoodWe SEMS+ Australia for Home Assistant

A Home Assistant custom integration for Australian GoodWe accounts hosted on
[SEMS+ Australia](https://au-semsplus.goodwe.com/).

> [!IMPORTANT]
> This is an Australia-only fork of
> [TimSoethout/goodwe-sems-home-assistant](https://github.com/TimSoethout/goodwe-sems-home-assistant).
> It uses the Australian SEMS+ authentication and API services. Accounts hosted
> in other regions are not supported; use the upstream integration instead.

## Features

- Creates one Home Assistant config entry for each selected SEMS+ station.
- Groups station, inverter, and battery telemetry under the appropriate devices.
- Provides generation, consumption, grid, battery, energy, and inverter sensors
  when those values are available from SEMS+ Australia.
- Polls the SEMS+ cloud API every five minutes and augments station power-flow
  sensors with the live MQTT feed between polls.

This integration depends on undocumented GoodWe cloud APIs. GoodWe may change
them without notice, and some entities may not be available for every inverter.

## Installation

### HACS

This fork is not included in the default HACS repository list. Add it as a
custom repository:

1. Open HACS in Home Assistant.
2. Select **Integrations**, open the menu, and choose **Custom repositories**.
3. Enter `https://github.com/tgxn/goodwe-sems-home-assistant` and select the
   **Integration** category.
4. Select **GoodWe SEMS+ (AU))**, download it, and restart Home Assistant.

### Manual

Copy `custom_components/sems_au` from this repository into the
`custom_components` directory in your Home Assistant configuration, then
restart Home Assistant.

## Configuration

1. In Home Assistant, go to **Settings > Devices & services**.
2. Select **Add integration** and search for **GoodWe SEMS+ Australia**.
3. Sign in with the credentials used at
   [SEMS+ Australia](https://au-semsplus.goodwe.com/).
4. Select the station to add. Run **Add integration** again to add another
   station from the same account.

A read-only visitor account is recommended. This integration does not expose
inverter or battery controls.

## Troubleshooting

Enable debug logging from the integration page in Home Assistant, reproduce the
problem, then disable debug logging to download the log file. You can also add:

```yaml
logger:
  default: info
  logs:
    custom_components.sems_au: debug
```

SEMS+ can respond slowly or temporarily reject requests. The integration will
retry during the next update cycle.

When reporting an API problem, include debug logs and, when possible, a
redacted HAR captured from the Australian SEMS+ website. Remove passwords,
cookies, authorization headers, tokens, email addresses, station IDs, and
inverter serial numbers before sharing it.

## Screenshots

![SEMS integration details](images/sems-details.webp)

![Finding the integration](images/search-integration.webp)

![Integration configuration](images/integration-flow.webp)

## Development

Install the test dependencies and run the checks from the repository root:

```bash
python -m pip install -r requirements.test.txt
python -m pytest tests/ -v
ruff check custom_components/sems_au/
ruff format --check custom_components/sems_au/
mypy custom_components/sems_au/ --ignore-missing-imports --python-version 3.13
```

## Credits

This project is an Australian-region fork of
[TimSoethout/goodwe-sems-home-assistant](https://github.com/TimSoethout/goodwe-sems-home-assistant).
The upstream project was inspired by
[Sprk-nl/goodwe_sems_portal_scraper](https://github.com/Sprk-nl/goodwe_sems_portal_scraper)
and [bouwew/sems2mqtt](https://github.com/bouwew/sems2mqtt).

This project is not affiliated with or endorsed by GoodWe.
