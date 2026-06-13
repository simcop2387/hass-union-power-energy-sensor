# Union Power Energy Sensor — Home Assistant Integration

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)

A Home Assistant custom integration that connects to the Union Power member portal to download hourly electricity usage data. Populates historical data into HA's recorder for use with the Energy Dashboard.

## Features

- **Energy Dashboard Integration** — hourly + daily statistics compatible with HA Energy Dashboard
- **Net Metering Support** — tracks consumption and return-to-grid (solar) separately
- **Configurable Polling** — adjustable poll interval (60-1440 minutes)
- **Manual Import Service** — import custom date ranges on demand
- **90-Day Initial Backfill** — automatically imports history on first setup

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
3. Enter your account number, password, and desired poll interval

## Services

### `union_power.import_range`

Import historical usage data for a custom date range.

```yaml
service: union_power.import_range
data:
  start_date: "2026-01-01"
  end_date: "2026-03-01"
```

## License

MIT
