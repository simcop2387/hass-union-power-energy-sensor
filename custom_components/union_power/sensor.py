"""Union Power energy sensor platform."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from typing import Any, Dict, List, Optional

from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_last_statistics,
    get_start_time,
    statistics_during_period,
)
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMetaData,
)
from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
    UpdateFailed,
)
from homeassistant.util.unit_conversion import EnergyConverter

try:
    from homeassistant.components.recorder.models import StatisticMeanType
except ImportError:
    from enum import Enum

    class StatisticMeanType(str, Enum):
        NONE = "none"
        MEAN = "mean"
        MAX = "max"
        MIN = "min"


from .api import IntervalUsage, UnionPowerAPI
from .exceptions import (
    UnionPowerAuthenticationError,
    UnionPowerConnectionError,
    UnionPowerError,
)

from .const import (
    BASE_URL,
    DOMAIN,
    CONF_ACCOUNT_NUMBER,
    CONF_SUMMER_RATE_TIER1,
    CONF_SUMMER_RATE_TIER2,
    CONF_WINTER_RATE_TIER1,
    CONF_WINTER_RATE_TIER2,
    SUMMER_MONTHS,
    TIER_THRESHOLD_KWH,
    DATA_LAG_DAYS,
    HISTORICAL_IMPORT_DAYS,
    ENERGY_SENSOR_KEY,
    ATTR_LAST_READING_TIME,
    ATTR_ACCOUNT_NUMBER,
    STAT_CONSUMPTION_HOURLY,
    STAT_CONSUMPTION_DAILY,
    STAT_RETURN_HOURLY,
    STAT_RETURN_DAILY,
    STAT_COST_HOURLY,
    STAT_COST_DAILY,
    )

_LOGGER = logging.getLogger(__name__)

def _log(level: str, msg: str, *args: Any) -> None:
    getattr(_LOGGER, level)(msg, *args)


def _calculate_cost(
    kwh: float,
    cumulative_before: float,
    month: int,
    rates: Dict[str, float],
) -> float:
    """Calculate cost for a single kWh increment using seasonal tiered rates.

    Args:
        kwh: kWh consumed in this interval
        cumulative_before: total kWh consumed in this month before this interval
        month: calendar month (1-12)
        rates: dict with CONF_SUMMER_RATE_TIER1, CONF_SUMMER_RATE_TIER2,
               CONF_WINTER_RATE_TIER1, CONF_WINTER_RATE_TIER2

    Returns:
        Cost in USD for this interval
    """
    if month in SUMMER_MONTHS:
        tier1 = rates.get(CONF_SUMMER_RATE_TIER1)
        tier2 = rates.get(CONF_SUMMER_RATE_TIER2)
    else:
        tier1 = rates.get(CONF_WINTER_RATE_TIER1)
        tier2 = rates.get(CONF_WINTER_RATE_TIER2)

    if tier1 is None:
        return 0.0

    remaining_tier1 = max(0.0, TIER_THRESHOLD_KWH - cumulative_before)

    if remaining_tier1 <= 0:
        kwh_tier2 = kwh
        kwh_tier1 = 0.0
    else:
        kwh_tier1 = min(kwh, remaining_tier1)
        kwh_tier2 = kwh - kwh_tier1

    cost = kwh_tier1 * tier1
    if tier2 is not None and kwh_tier2 > 0:
        cost += kwh_tier2 * tier2

    return cost


def _rates_configured(rates: Dict[str, float]) -> bool:
    """Return True if at least one rate is configured."""
    return any(
        rates.get(k) is not None
        for k in (CONF_SUMMER_RATE_TIER1, CONF_SUMMER_RATE_TIER2,
                   CONF_WINTER_RATE_TIER1, CONF_WINTER_RATE_TIER2)
    )


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Union Power sensor platform."""
    coordinator: UnionPowerDataUpdateCoordinator = config_entry.runtime_data
    async_add_entities(
        [
            UnionPowerEnergySensor(coordinator, config_entry),
        ]
    )


class UnionPowerDataUpdateCoordinator(DataUpdateCoordinator):
    """Manages fetching Union Power data and populating HA statistics."""

    def __init__(
        self,
        hass: HomeAssistant,
        api: UnionPowerAPI,
        update_interval: timedelta,
        config_entry: ConfigEntry,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_{config_entry.entry_id}",
            update_interval=update_interval,
        )
        self.api = api
        self.account_number = config_entry.data.get(CONF_ACCOUNT_NUMBER, "unknown")
        self.rates = {
            CONF_SUMMER_RATE_TIER1: config_entry.data.get(CONF_SUMMER_RATE_TIER1),
            CONF_SUMMER_RATE_TIER2: config_entry.data.get(CONF_SUMMER_RATE_TIER2),
            CONF_WINTER_RATE_TIER1: config_entry.data.get(CONF_WINTER_RATE_TIER1),
            CONF_WINTER_RATE_TIER2: config_entry.data.get(CONF_WINTER_RATE_TIER2),
        }

    async def _async_update_data(self) -> Dict[str, Any]:
        """Return stored data. Fetching is done by background tasks, not here."""
        return self.data or {}

    async def run_fetch_cycle(self) -> None:
        """Run a full fetch cycle: login, fetch, insert stats, update coordinator data."""
        try:
            _log("warning", "Running fetch cycle")
            await self.api.login()
            _log("warning", "Login successful")

            ha_tz = ZoneInfo(self.hass.config.time_zone)
            now = datetime.now(tz=ha_tz)
            end_date = now.replace(hour=0, minute=0, second=0, microsecond=0)
            _log("warning", "Data window: now=%s, end_date=%s (tz=%s)", now.date(), end_date.date(), ha_tz)

            last_stat = await self._get_last_nonzero_stat(
                STAT_CONSUMPTION_HOURLY.format(account=self.account_number)
            )
            _log("warning", "Last non-zero stat timestamp: %s", last_stat)

            if last_stat is None:
                start_date = end_date - timedelta(days=HISTORICAL_IMPORT_DAYS)
                _log("warning", "No prior stats found — initial import: %s → %s (%d days)", start_date.date(), end_date.date(), HISTORICAL_IMPORT_DAYS)
            else:
                start_date = datetime.fromtimestamp(last_stat, tz=ha_tz)
                _log("warning", "Incremental window: last_stat=%s, start_date=%s, end_date=%s", last_stat, start_date.date(), end_date.date())
                if start_date > end_date:
                    _log("warning", "No new data to fetch: start_date (%s) > end_date (%s)", start_date.date(), end_date.date())
                    return
                _log("warning", "Incremental update: %s → %s", start_date.date(), end_date.date())

            _log("warning", "Fetching interval data: %s → %s", start_date.date(), end_date.date())
            records = await self.api.get_interval_usage(start_date.replace(tzinfo=None), end_date.replace(tzinfo=None))
            _log("warning", "API returned %d records", len(records))

            if not records:
                _log("warning", "No interval data returned for %s → %s", start_date.date(), end_date.date())
                return

            _log("warning", "Inserting statistics for %d records", len(records))
            for i, rec in enumerate(records[:3]):
                _log("warning", "API record[%d]: ts=%s KWH=%.4f GKWH=%.4f", i, rec.timestamp, rec.used_from_grid, rec.total_generation)
            if len(records) > 3:
                for i in range(len(records) - 3, len(records)):
                    _log("warning", "API record[%d]: ts=%s KWH=%.4f GKWH=%.4f", i, records[i].timestamp, records[i].used_from_grid, records[i].total_generation)
            await self._insert_statistics(records)
            _log("warning", "Statistics inserted successfully")

            monthly_total, last_reading_time = self._compute_monthly(records)
            _log("warning", "Last reading: %s, month-to-date: %.3f kWh", last_reading_time, monthly_total)

            self.data = {
                ENERGY_SENSOR_KEY: monthly_total,
                ATTR_LAST_READING_TIME: last_reading_time,
                ATTR_ACCOUNT_NUMBER: self.account_number,
            }
            _log("warning", "Fetch cycle complete: %d records, last reading: %s", len(records), last_reading_time)

        except (UnionPowerAuthenticationError, UnionPowerConnectionError) as e:
            _log("error", "Fetch cycle failed - authentication/connection: %s", e)
        except UnionPowerError as e:
            _log("error", "Fetch cycle failed - API error: %s", e)
        except Exception as e:
            _log("exception", "Fetch cycle failed - unexpected error: %s", e)

    async def import_range(
        self, start_date: datetime, end_date: datetime
    ) -> int:
        """Manually import data for a custom date range."""
        _log("warning", "import_range starting: %s → %s", start_date.date(), end_date.date())
        await self.api.login()
        _log("warning", "import_range login OK, fetching data...")
        records = await self.api.get_interval_usage(start_date, end_date)
        _log("warning", "import_range got %d records", len(records))

        if not records:
            _log("warning", "No data returned for range %s → %s", start_date.date(), end_date.date())
            return 0

        await self._insert_statistics(records)
        _log("warning", "import_range inserted %d records for %s → %s", len(records), start_date.date(), end_date.date())
        return len(records)

    async def fill_all_stats(self) -> int:
        """Create cost statistics from all existing consumption data."""
        if not _rates_configured(self.rates):
            _log("warning", "fill_all_stats: no rates configured, nothing to do")
            return 0

        _log("warning", "fill_all_stats: rates = %s", self.rates)

        stat_id = STAT_CONSUMPTION_HOURLY.format(account=self.account_number)

        start_time = datetime(2020, 1, 1, tzinfo=timezone.utc)
        end_time = datetime.now(tz=timezone.utc)

        _log("warning", "fill_all_stats: reading stats from %s to %s", start_time, end_time)

        stats = await self.hass.async_add_executor_job(
            statistics_during_period,
            self.hass,
            start_time,
            end_time,
            {stat_id},
            "hour",
            None,
            {"sum"},
        )

        rows = stats.get(stat_id, [])
        if not rows:
            _log("warning", "fill_all_stats: no existing stats found")
            return 0

        _log("warning", "fill_all_stats: found %d existing stats, creating cost stats", len(rows))

        ha_tz = ZoneInfo(self.hass.config.time_zone)
        def _localize(dt: datetime) -> datetime:
            if dt.tzinfo is None:
                return dt.replace(tzinfo=ha_tz)
            return dt

        cost_hourly: List[StatisticData] = []
        cost_daily_map: Dict[str, float] = {}
        monthly_cumulative: Dict[str, float] = {}

        for row in rows:
            dt = datetime.fromtimestamp(row["start"], tz=timezone.utc).astimezone(ha_tz)
            kwh = row.get("sum", 0.0) or 0.0  # sum = per-period kWh, never use state (HA normalizes to cumulative)

            month_key = dt.strftime("%Y-%m")
            cumulative_before = monthly_cumulative.get(month_key, 0.0)
            cost_state = _calculate_cost(kwh, cumulative_before, dt.month, self.rates)
            monthly_cumulative[month_key] = cumulative_before + kwh

            cost_hourly.append(
                StatisticData(start=dt, state=cost_state, sum=cost_state)
            )
            day_key = dt.strftime("%Y-%m-%d")
            if day_key not in cost_daily_map:
                cost_daily_map[day_key] = 0.0
            cost_daily_map[day_key] += cost_state

        cost_daily: List[StatisticData] = []
        for day_key in sorted(cost_daily_map.keys()):
            dt = _localize(datetime.strptime(day_key, "%Y-%m-%d"))
            cost_daily.append(
                StatisticData(start=dt, state=cost_daily_map[day_key], sum=cost_daily_map[day_key])
            )

        cost_hourly_meta = StatisticMetaData(
            mean_type=StatisticMeanType.NONE,
            has_sum=True,
            name=f"Union Power Cost Hourly - {self.account_number}",
            source=DOMAIN,
            statistic_id=STAT_COST_HOURLY.format(account=self.account_number),
            unit_class=None,
            unit_of_measurement="USD",
        )
        async_add_external_statistics(self.hass, cost_hourly_meta, cost_hourly)
        _log("warning", "fill_all_stats: created %d hourly cost stats", len(cost_hourly))

        cost_daily_meta = StatisticMetaData(
            mean_type=StatisticMeanType.NONE,
            has_sum=True,
            name=f"Union Power Cost Daily - {self.account_number}",
            source=DOMAIN,
            statistic_id=STAT_COST_DAILY.format(account=self.account_number),
            unit_class=None,
            unit_of_measurement="USD",
        )
        async_add_external_statistics(self.hass, cost_daily_meta, cost_daily)
        _log("warning", "fill_all_stats: created %d daily cost stats", len(cost_daily))

        return len(cost_hourly) + len(cost_daily)

    async def _get_last_nonzero_stat(self, statistic_id: str) -> Optional[float]:
        """Scan backwards from today to find the last stat with sum > 0 (actual usage)."""
        ha_tz = ZoneInfo(self.hass.config.time_zone)
        stats = await self.hass.async_add_executor_job(
            statistics_during_period,
            self.hass,
            datetime(2020, 1, 1, tzinfo=timezone.utc),
            datetime.now(tz=timezone.utc),
            {statistic_id},
            "hour",
            None,
            {"sum"},
        )
        rows = stats.get(statistic_id, [])
        _log("warning", "_get_last_nonzero_stat[%s]: scanned %d total rows", statistic_id, len(rows))
        for i, row in enumerate(reversed(rows)):
            row_sum = row.get("sum", 0.0) or 0.0
            ts = row.get("start")
            local_dt = datetime.fromtimestamp(ts, tz=ha_tz) if ts else None
            _log("warning", "_get_last_nonzero_stat[%s]: row[%d/%d] start=%s (local: %s) sum=%.6f state=%s", statistic_id, i + 1, len(rows), ts, local_dt, row_sum, row.get("state", "MISSING"))
            if row_sum > 0:
                _log("warning", "_get_last_nonzero_stat[%s]: found last non-zero at %s (local: %s, sum=%.4f kWh)", statistic_id, ts, local_dt, row_sum)
                return ts
        _log("warning", "_get_last_nonzero_stat[%s]: no non-zero stats found in %d rows", statistic_id, len(rows))
        return None

    async def _insert_statistics(self, records: List[IntervalUsage]) -> None:
        """Insert hourly and daily statistics into the HA recorder."""
        daily: Dict[str, Dict[str, float]] = {}
        hourly_consumption: List[IntervalUsage] = []
        hourly_return: List[IntervalUsage] = []

        for rec in records:
            day_key = rec.timestamp[:10]
            if day_key not in daily:
                daily[day_key] = {"consumption": 0.0, "return": 0.0}
            daily[day_key]["consumption"] += rec.used_from_grid
            daily[day_key]["return"] += rec.total_generation

            hourly_consumption.append(rec)
            if rec.total_generation > 0:
                hourly_return.append(rec)

        _log("warning", "_insert_statistics: %d records, date range %s → %s", len(records), records[0].timestamp if records else "N/A", records[-1].timestamp if records else "N/A")
        for i, rec in enumerate(records[:3]):
            _log("warning", "_insert_statistics: record[%d] ts=%s KWH=%.4f GKWH=%.4f", i, rec.timestamp, rec.used_from_grid, rec.total_generation)
        if len(records) > 3:
            for i in range(len(records) - 3, len(records)):
                _log("warning", "_insert_statistics: record[%d] ts=%s KWH=%.4f GKWH=%.4f", i, records[i].timestamp, records[i].used_from_grid, records[i].total_generation)

        cons_unit_class = EnergyConverter.UNIT_CLASS
        cons_unit = UnitOfEnergy.KILO_WATT_HOUR

        cons_hourly_meta = StatisticMetaData(
            mean_type=StatisticMeanType.NONE,
            has_sum=True,
            name=f"Union Power Energy Hourly Usage - {self.account_number}",
            source=DOMAIN,
            statistic_id=STAT_CONSUMPTION_HOURLY.format(account=self.account_number),
            unit_class=cons_unit_class,
            unit_of_measurement=cons_unit,
        )

        cons_daily_meta = StatisticMetaData(
            mean_type=StatisticMeanType.NONE,
            has_sum=True,
            name=f"Union Power Energy Daily Usage - {self.account_number}",
            source=DOMAIN,
            statistic_id=STAT_CONSUMPTION_DAILY.format(account=self.account_number),
            unit_class=cons_unit_class,
            unit_of_measurement=cons_unit,
        )

        ret_hourly_meta = StatisticMetaData(
            mean_type=StatisticMeanType.NONE,
            has_sum=True,
            name=f"Union Power Energy Hourly Return - {self.account_number}",
            source=DOMAIN,
            statistic_id=STAT_RETURN_HOURLY.format(account=self.account_number),
            unit_class=cons_unit_class,
            unit_of_measurement=cons_unit,
        )

        ret_daily_meta = StatisticMetaData(
            mean_type=StatisticMeanType.NONE,
            has_sum=True,
            name=f"Union Power Energy Daily Return - {self.account_number}",
            source=DOMAIN,
            statistic_id=STAT_RETURN_DAILY.format(account=self.account_number),
            unit_class=cons_unit_class,
            unit_of_measurement=cons_unit,
        )

        cost_hourly_meta = None
        cost_daily_meta = None
        if _rates_configured(self.rates):
            cost_hourly_meta = StatisticMetaData(
                mean_type=StatisticMeanType.NONE,
                has_sum=True,
                name=f"Union Power Cost Hourly - {self.account_number}",
                source=DOMAIN,
                statistic_id=STAT_COST_HOURLY.format(account=self.account_number),
                unit_class=None,
                unit_of_measurement="USD",
            )
            cost_daily_meta = StatisticMetaData(
                mean_type=StatisticMeanType.NONE,
                has_sum=True,
                name=f"Union Power Cost Daily - {self.account_number}",
                source=DOMAIN,
                statistic_id=STAT_COST_DAILY.format(account=self.account_number),
                unit_class=None,
                unit_of_measurement="USD",
            )

        ha_tz = ZoneInfo(self.hass.config.time_zone)

        def _localize(dt: datetime) -> datetime:
            if dt.tzinfo is None:
                return dt.replace(tzinfo=ha_tz)
            return dt

        # Track cumulative kWh per month for tiered pricing (hourly)
        hourly_cumulative: Dict[str, float] = {}

        # Build hourly consumption stats (per-period)
        cons_hourly_stats: List[StatisticData] = []
        cost_hourly_stats: List[StatisticData] = []
        hourly_cost_by_day: Dict[str, float] = {}
        for rec in hourly_consumption:
            dt = _localize(self.api.parse_timestamp(rec.timestamp))
            month_key = dt.strftime("%Y-%m")
            day_key = rec.timestamp[:10]
            cumulative_before = hourly_cumulative.get(month_key, 0.0)

            cons_hourly_stats.append(
                StatisticData(start=dt, state=rec.used_from_grid, sum=rec.used_from_grid)
            )
            if _rates_configured(self.rates):
                cost_state = _calculate_cost(rec.used_from_grid, cumulative_before, dt.month, self.rates)
                cost_hourly_stats.append(
                    StatisticData(start=dt, state=cost_state, sum=cost_state)
                )
                hourly_cost_by_day[day_key] = hourly_cost_by_day.get(day_key, 0.0) + cost_state
            hourly_cumulative[month_key] = cumulative_before + rec.used_from_grid

        # Build daily consumption stats (per-period)
        cons_daily_stats: List[StatisticData] = []
        cost_daily_stats: List[StatisticData] = []
        for day_key in sorted(daily.keys()):
            dt = _localize(datetime.strptime(day_key, "%m/%d/%Y"))
            day_total = daily[day_key]["consumption"]
            cons_daily_stats.append(
                StatisticData(start=dt, state=day_total, sum=day_total)
            )
            if _rates_configured(self.rates):
                cost_daily_stats.append(
                    StatisticData(start=dt, state=hourly_cost_by_day.get(day_key, 0.0), sum=hourly_cost_by_day.get(day_key, 0.0))
                )

        # Build hourly return stats (per-period)
        ret_hourly_stats: List[StatisticData] = []
        for rec in hourly_return:
            dt = _localize(self.api.parse_timestamp(rec.timestamp))
            ret_hourly_stats.append(
                StatisticData(start=dt, state=rec.total_generation, sum=rec.total_generation)
            )

        # Build daily return stats (per-period)
        ret_daily_stats: List[StatisticData] = []
        for day_key in sorted(daily.keys()):
            dt = _localize(datetime.strptime(day_key, "%m/%d/%Y"))
            day_total = daily[day_key]["return"]
            ret_daily_stats.append(
                StatisticData(start=dt, state=day_total, sum=day_total)
            )

        # Insert into recorder
        if cons_hourly_stats:
            _log("warning", "Inserting %d hourly consumption stats [%s]:", len(cons_hourly_stats), cons_hourly_meta["statistic_id"])
            for s in cons_hourly_stats[:3]:
                _log("warning", "  cons_hourly: start=%s state=%.4f sum=%.4f", s["start"], s["state"], s["sum"])
            if len(cons_hourly_stats) > 3:
                for s in cons_hourly_stats[-3:]:
                    _log("warning", "  cons_hourly: start=%s state=%.4f sum=%.4f", s["start"], s["state"], s["sum"])
            async_add_external_statistics(self.hass, cons_hourly_meta, cons_hourly_stats)

        if cons_daily_stats:
            _log("warning", "Inserting %d daily consumption stats [%s]:", len(cons_daily_stats), cons_daily_meta["statistic_id"])
            for s in cons_daily_stats[:3]:
                _log("warning", "  cons_daily: start=%s state=%.4f sum=%.4f", s["start"], s["state"], s["sum"])
            async_add_external_statistics(self.hass, cons_daily_meta, cons_daily_stats)

        if ret_hourly_stats:
            _log("warning", "Inserting %d hourly return stats [%s]:", len(ret_hourly_stats), ret_hourly_meta["statistic_id"])
            for s in ret_hourly_stats[:3]:
                _log("warning", "  ret_hourly: start=%s state=%.4f sum=%.4f", s["start"], s["state"], s["sum"])
            async_add_external_statistics(self.hass, ret_hourly_meta, ret_hourly_stats)

        if ret_daily_stats:
            _log("warning", "Inserting %d daily return stats [%s]:", len(ret_daily_stats), ret_daily_meta["statistic_id"])
            for s in ret_daily_stats[:3]:
                _log("warning", "  ret_daily: start=%s state=%.4f sum=%.4f", s["start"], s["state"], s["sum"])
            async_add_external_statistics(self.hass, ret_daily_meta, ret_daily_stats)

        if cost_hourly_stats and cost_hourly_meta:
            _log("warning", "Inserting %d hourly cost stats [%s]:", len(cost_hourly_stats), cost_hourly_meta["statistic_id"])
            for s in cost_hourly_stats[:3]:
                _log("warning", "  cost_hourly: start=%s state=%.4f sum=%.4f", s["start"], s["state"], s["sum"])
            async_add_external_statistics(self.hass, cost_hourly_meta, cost_hourly_stats)

        if cost_daily_stats and cost_daily_meta:
            _log("warning", "Inserting %d daily cost stats [%s]:", len(cost_daily_stats), cost_daily_meta["statistic_id"])
            for s in cost_daily_stats[:3]:
                _log("warning", "  cost_daily: start=%s state=%.4f sum=%.4f", s["start"], s["state"], s["sum"])
            async_add_external_statistics(self.hass, cost_daily_meta, cost_daily_stats)

        _log("warning", "_insert_statistics complete")

    @staticmethod
    def _compute_monthly(records: List[IntervalUsage]) -> tuple[float, str]:
        """Compute current month-to-date total and last reading time."""
        now = datetime.now()
        monthly_total = 0.0
        last_reading_time = ""

        for rec in records:
            dt = UnionPowerAPI.parse_timestamp(rec.timestamp)
            if dt.year == now.year and dt.month == now.month:
                monthly_total += rec.used_from_grid
                last_reading_time = rec.timestamp

        return round(monthly_total, 3), last_reading_time


class UnionPowerEnergySensor(CoordinatorEntity, SensorEntity):
    """Representation of a Union Power energy sensor."""

    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_icon = "mdi:lightning-bolt"

    def __init__(
        self,
        coordinator: UnionPowerDataUpdateCoordinator,
        config_entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator)
        self._config_entry = config_entry
        self._attr_unique_id = f"{config_entry.entry_id}_energy"
        account = config_entry.data.get(CONF_ACCOUNT_NUMBER, "Unknown")
        self._attr_name = f"Union Power Monthly Usage - {account}"

    @property
    def available(self) -> bool:
        return self.coordinator.last_update_success and self.native_value is not None

    @property
    def native_value(self) -> Optional[float]:
        if not self.coordinator.data:
            return None
        value = self.coordinator.data.get(ENERGY_SENSOR_KEY)
        if value is None:
            return None
        try:
            return float(value)
        except (ValueError, TypeError):
            return None

    @property
    def extra_state_attributes(self) -> Dict[str, Any]:
        attrs: Dict[str, Any] = {
            ATTR_ACCOUNT_NUMBER: self._config_entry.data.get(CONF_ACCOUNT_NUMBER),
        }
        if self.coordinator.data:
            last = self.coordinator.data.get(ATTR_LAST_READING_TIME)
            if last:
                attrs[ATTR_LAST_READING_TIME] = last
        return attrs

    @property
    def device_info(self) -> Dict[str, Any]:
        return {
            "identifiers": {(DOMAIN, self._config_entry.entry_id)},
            "name": f"Union Power Energy ({self._config_entry.data.get(CONF_ACCOUNT_NUMBER, 'Unknown')})",
            "manufacturer": "Union Power Co-op",
            "model": "Energy Monitor",
            "configuration_url": f"{BASE_URL}",
        }
