# Union Power Energy Sensor — Home Assistant Integration

## Overview

Home Assistant custom integration that connects to the Union Power member portal to download hourly/daily electricity usage information. Populates historical data into HA's recorder for use with the Energy Dashboard.

## Features

- Energy Dashboard integration with hourly + daily statistics
- Net metering support (consumption + return to grid)
- Configurable polling intervals
- Manual import service for custom date ranges
- 90-day initial backfill on first setup

## Data Source

- **Hourly interval API** — primary data source, 60-minute intervals
- Account IDs derived from account number (split last 3 digits)
- Data has 1-2 day lag due to daily batch processing

## Installation

### HACS (Recommended)

1. Open HACS → Custom repositories
2. Add this repository URL, select "Integration"
3. Search for "Union Power Energy" and download

### Manual

1. Copy `custom_components/union_power/` to your HA `config/custom_components/` directory
2. Restart Home Assistant

## Configuration

1. Settings → Devices & Services → Add Integration
2. Search for "Union Power Energy"
3. Enter:
   - Account Number (e.g. `5497637001`)
   - Password
   - Poll interval (default: 360 minutes)

## Services

### `union_power.import_range`

Import historical usage data for a custom date range.

| Field | Description |
|-------|-------------|
| `start_date` | Start date (YYYY-MM-DD) |
| `end_date` | End date (YYYY-MM-DD) |

## API Endpoints

| Endpoint | Purpose |
|----------|---------|
| `GET /Customer-Login` | Fetch login page tokens |
| `POST /Customer-Login` | Submit credentials |
| `POST .../GetIntervalData` | Hourly kWh + weather data |

## Architecture

```
custom_components/union_power/
  __init__.py          # Setup entry, create API + coordinator, register services
  api.py               # UnionPowerAPI — async login, session mgmt, data fetch
  config_flow.py       # HA config UI: account, password, poll interval
  const.py             # DOMAIN, config keys, defaults
  exceptions.py        # UnionPowerError hierarchy
  sensor.py            # Coordinator + SensorEntity + statistics import
  services.yaml        # Service definitions
  strings.json         # UI strings
  translations/en.json # English translations
  manifest.json        # Domain, deps: aiohttp, beautifulsoup4
```

## Data Flow

```
Coordinator poll (every N hours)
  → login() [fresh every time]
  → fetch hourly data for window
  → build cumulative sums
  → async_add_external_statistics (hourly + daily)
  → update sensor entity (monthly total)

Service: union_power.import_range
  → user picks start/end date
  → fetch + insert/overwrite stats for that range
```

## Statistic IDs

| Statistic | ID Pattern |
|-----------|-----------|
| Hourly consumption | `union_power:energy_hourly_{account}` |
| Daily consumption | `union_power:energy_daily_{account}` |
| Hourly return | `union_power:energy_return_hourly_{account}` |
| Daily return | `union_power:energy_return_daily_{account}` |

## Implementation Checklist

- [x] Skeleton files (manifest, const, exceptions, strings, translations, hacs, LICENSE, README)
- [ ] API layer (`api.py`) — async login, interval data fetch, parsing
- [ ] Config flow (`config_flow.py`)
- [ ] Sensor + Coordinator (`sensor.py`) — statistics import, entity
- [ ] Init + Service (`__init__.py`, `services.yaml`)
- [ ] Test in HA instance
