"""Constants for the Union Power integration."""

DOMAIN = "union_power"

# Configuration keys
CONF_ACCOUNT_NUMBER = "account_number"
CONF_PASSWORD = "password"
CONF_COST_PER_KWH = "cost_per_kwh"

# Poll interval — locked to daily since data only updates once per day
POLL_INTERVAL_MINUTES = 1440

# API constants
BASE_URL = "https://services.union-power.com/onlineportal"
DEFAULT_TIMEOUT = 30  # seconds
DATA_LAG_DAYS = 2  # Data is delayed by this many days

# Historical import
HISTORICAL_IMPORT_DAYS = 90  # Initial backfill window

# Sensor constants
ENERGY_SENSOR_KEY = "current_energy_usage"
ATTR_LAST_READING_TIME = "last_reading_time"
ATTR_ACCOUNT_NUMBER = "account_number"

# Statistic name patterns
STAT_CONSUMPTION_HOURLY = "union_power:energy_hourly_{account}"
STAT_CONSUMPTION_DAILY = "union_power:energy_daily_{account}"
STAT_RETURN_HOURLY = "union_power:energy_return_hourly_{account}"
STAT_RETURN_DAILY = "union_power:energy_return_daily_{account}"
STAT_COST_HOURLY = "union_power:cost_hourly_{account}"
STAT_COST_DAILY = "union_power:cost_daily_{account}"
