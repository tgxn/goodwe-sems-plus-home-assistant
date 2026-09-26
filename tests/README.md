# Tests

Tests for the GoodWe SEMS+ integration (`custom_components/sems_plus`). They
run against Home Assistant's test harness with the API client mocked, so no
network access or SEMS+ account is needed.

| File                  | Covers                                                                                                        |
| --------------------- | ------------------------------------------------------------------------------------------------------------- |
| `test_api.py`         | API client: request URLs and bodies, token refresh, pagination, MQTT payload decoding and live feed lifecycle |
| `test_mapping.py`     | Value mapping: factor flattening, flow signs, unit conversion, statistics totals, status labels               |
| `test_init.py`        | Setup, devices and entity values, live updates, live feed health entities, unload                             |
| `test_entities.py`    | Entity naming rules and English translations                                                                  |
| `test_config_flow.py` | Config flow (credentials, station, settings), errors and options flow                                         |
| `conftest.py`         | Shared fixtures: config entry and a mocked `SemsPlusClient`                                                   |
| `fixtures/`           | Simplified SEMS+ API responses with generic placeholder values                                                |

## Running

```bash
python -m pip install -r requirements.test.txt
python -m pytest tests -v
```

Home Assistant 2026.8+ (Python 3.14) is required.
