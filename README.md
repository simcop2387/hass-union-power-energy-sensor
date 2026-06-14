# Union Power Energy Sensor — Home Assistant Integration

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)

A Home Assistant custom integration that connects to the Union Power member portal to download hourly electricity usage data and populate HA's Energy Dashboard with consumption, return-to-grid, and cost statistics.

## Features

- **Energy Dashboard Integration** — hourly + daily statistics for consumption, return-to-grid, and cost
- **Net Metering Support** — tracks consumption (`KWH`) and return-to-grid (`GKWH`) separately
- **Tiered Seasonal Cost Tracking** — 4 configurable rates (Summer/Winter × Tier 1/Tier 2), 1000 kWh monthly threshold
- **Non-blocking Startup** — all data fetching runs via background tasks
- **Manual Import Service** — import custom date ranges on demand
- **90-Day Initial Backfill** — automatically imports history on first setup
- **Daily Polling** — scheduled fetch at 6:00 AM local time (data updates once per day)

## Installation

### HACS (Recommended)

1. Open HACS → Custom repositories
2. Add this repository URL, select "Integration"
3. Search for "Union Power Energy" and download
4. Restart Home Assistant

### Manual

1. Copy `custom_components/union_power/` to your HA `config/custom_components/` directory
2. Restart Home Assistant

## Configuration

1. Settings → Devices & Services → Add Integration
2. Search for "Union Power Energy"
3. Enter your account number, password, and optional seasonal rates:
   - **Summer Rate Tier 1**: $/kWh for first 1000 kWh/month (June–October)
   - **Summer Rate Tier 2**: $/kWh for usage over 1000 kWh/month (June–October)
   - **Winter Rate Tier 1**: $/kWh for first 1000 kWh/month (November–May)
   - **Winter Rate Tier 2**: $/kWh for usage over 1000 kWh/month (November–May)

Rate fields are optional. If none are set, cost statistics won't be calculated.

## How It Works

### Data Flow

1. On startup, a background task fetches the last 90 days of hourly data (or incrementally fetches new data if stats already exist)
2. Daily at 6:00 AM local time, a scheduled fetch picks up any new data since the last run
3. Hourly records are aggregated into hourly and daily statistics for consumption, return-to-grid, and cost
4. Statistics are inserted as **external statistics** into HA's recorder using `async_add_external_statistics`

### Statistics

| Statistic ID Pattern | Description | Unit |
|---|---|---|
| `union_power:energy_hourly_{account}` | Hourly consumption | kWh |
| `union_power:energy_daily_{account}` | Daily consumption | kWh |
| `union_power:energy_return_hourly_{account}` | Hourly return-to-grid | kWh |
| `union_power:energy_return_daily_{account}` | Daily return-to-grid | kWh |
| `union_power:cost_hourly_{account}` | Hourly cost | USD |
| `union_power:cost_daily_{account}` | Daily cost | USD |

All statistics use `has_sum=True` with cumulative `sum` values. The `state` field holds the per-period value (kWh or $ for that hour/day). HA computes displayed changes from consecutive `sum` differences.

### Cumulative Sum Continuity

The integration maintains cumulative continuity across fetch cycles:
- Before inserting new data, it queries the stored cumulative `sum` from 1 hour **before** the fetch range
- New rows continue from that cumulative value, avoiding double-counting
- When importing a custom range, post-range rows are automatically adjusted to continue from the new boundary

### Tiered Pricing

Cost is calculated per hourly interval using seasonal tiered rates:
- **Summer**: June (6) through October (10)
- **Winter**: November (11) through May (5)
- **Tier 1**: First 1000 kWh per calendar month
- **Tier 2**: Usage above 1000 kWh per calendar month
- Monthly cumulative kWh resets at the start of each calendar month
- Daily cost = sum of hourly costs for that day

## Services

### `union_power.import_range`

Import historical usage data for a custom date range. Post-range statistics are automatically adjusted to maintain cumulative continuity.

```yaml
service: union_power.import_range
data:
  start_date: "2026-01-01"
  end_date: "2026-03-01"
```

### `union_power.fill_all_stats`

Recalculate all cost statistics from existing consumption data. Useful after changing rate configuration.

```yaml
service: union_power.fill_all_stats
```

### `union_power.trigger_fetch`

Manually trigger the normal fetch cycle (login, fetch today's data, insert statistics).

```yaml
service: union_power.trigger_fetch
```

## Sensors

| Sensor | Description |
|---|---|
| `sensor.union_power_monthly_usage_{account}` | Current month-to-date energy consumption in kWh |

## Troubleshooting

### Energy totals spike on startup

This was caused by overlap double-counting and has been fixed. If you still see spikes from before the fix, run `union_power.import_range` for the affected date range to rebuild the statistics with correct cumulative sums, then run `union_power.fill_all_stats` to recalculate costs.

### Cost values not showing

Ensure at least one seasonal rate is configured. Run `union_power.fill_all_stats` after setting rates to populate cost statistics from existing consumption data.

### "Detected code that accesses the database without the database executor"

All recorder calls use `get_instance(hass).async_add_executor_job()`. If you see this warning, you may be running an older version — update via HACS.

## License

MIT
