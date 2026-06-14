"""Union Power energy sensor platform."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from typing import Any, Dict, List, Optional

from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_last_statistics,
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

            cons_stat_id = STAT_CONSUMPTION_HOURLY.format(account=self.account_number)
            ret_stat_id = STAT_RETURN_HOURLY.format(account=self.account_number)

            last_ts, _ = await self._get_last_stat_with_sum(cons_stat_id, datetime(2020, 1, 1, tzinfo=ha_tz))

            if last_ts is None:
                start_date = end_date - timedelta(days=HISTORICAL_IMPORT_DAYS)
                _log("warning", "No prior stats found — initial import: %s → %s (%d days)", start_date.date(), end_date.date(), HISTORICAL_IMPORT_DAYS)
                overlap_cons_sum = 0.0
                overlap_ret_sum = 0.0
            else:
                start_date = datetime.fromtimestamp(last_ts, tz=ha_tz)
                # Backdate 1 day for overlap so the boundary row gets overwritten
                start_date = start_date - timedelta(days=1)
                _log("warning", "Incremental window (with 1-day overlap): last_stat=%s, start_date=%s, end_date=%s", last_ts, start_date.date(), end_date.date())
                if start_date > end_date:
                    _log("warning", "No new data to fetch: start_date (%s) > end_date (%s)", start_date.date(), end_date.date())
                    return
                _log("warning", "Incremental update: %s → %s", start_date.date(), end_date.date())

                # Get cumulative sums at the overlap point
                _, overlap_cons_sum = await self._get_last_stat_with_sum(cons_stat_id, start_date)
                _, overlap_ret_sum = await self._get_last_stat_with_sum(ret_stat_id, start_date)
                _log("warning", "Overlap cumulative sums: cons=%.4f ret=%.4f", overlap_cons_sum or 0.0, overlap_ret_sum or 0.0)

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
            await self._insert_statistics(records, overlap_cons_sum or 0.0, overlap_ret_sum or 0.0)
            _log("warning", "Statistics inserted successfully")

            monthly_total, last_reading_time = self._compute_monthly(records, now)
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

        # Get cumulative sum BEFORE the import range
        cons_stat_id = STAT_CONSUMPTION_HOURLY.format(account=self.account_number)
        ret_stat_id = STAT_RETURN_HOURLY.format(account=self.account_number)

        # Get the cumulative sum from the row just before our import range
        ha_tz = ZoneInfo(self.hass.config.time_zone)
        query_start = datetime(2020, 1, 1, tzinfo=ha_tz)
        last_ts, _ = await self._get_last_stat_with_sum(cons_stat_id, query_start)

        if last_ts is not None:
            # Get cumulative sum at the point just before our import range
            _, pre_cons_sum = await self._get_last_stat_with_sum(cons_stat_id, start_date)
            _, pre_ret_sum = await self._get_last_stat_with_sum(ret_stat_id, start_date)
            _log("warning", "import_range: pre-range cumulative sums: cons=%.4f ret=%.4f", pre_cons_sum or 0.0, pre_ret_sum or 0.0)
        else:
            pre_cons_sum = 0.0
            pre_ret_sum = 0.0

        await self._insert_statistics(records, pre_cons_sum or 0.0, pre_ret_sum or 0.0)
        _log("warning", "import_range inserted %d records for %s → %s", len(records), start_date.date(), end_date.date())

        # Adjust post-range rows to continue from new cumulative sums
        end_ts = end_date.replace(hour=23, minute=59, second=59)
        await self._adjust_post_range(cons_stat_id, end_ts)
        await self._adjust_post_range(ret_stat_id, end_ts)

        return len(records)

    async def _adjust_post_range(
        self,
        statistic_id: str,
        cutoff: datetime,
    ) -> None:
        """Adjust all stats after cutoff to continue from new cumulative sum.

        Extracts per-period from consecutive differences, rebuilds cumulative from new boundary.
        """
        stats = await get_instance(self.hass).async_add_executor_job(
            statistics_during_period,
            self.hass,
            cutoff,
            datetime.now(tz=timezone.utc),
            {statistic_id},
            "hour",
            None,
            {"sum"},
        )
        rows = stats.get(statistic_id, [])
        if not rows:
            return

        # Get the new cumulative sum at the cutoff (from our just-inserted data)
        new_boundary_sum = rows[0].get("sum", 0.0) or 0.0
        _log("warning", "_adjust_post_range[%s]: %d rows after cutoff, boundary sum=%.4f", statistic_id, len(rows), new_boundary_sum)

        # Extract per-period from consecutive differences and rebuild cumulative
        # Skip rows[0] — it's the boundary row we just inserted, already correct
        adjusted: List[StatisticData] = []
        cumsum = new_boundary_sum
        prev_sum = new_boundary_sum

        for row in rows[1:]:
            old_sum = row.get("sum", 0.0) or 0.0
            per_period = old_sum - prev_sum
            if per_period < 0:
                per_period = 0.0
            cumsum += per_period
            adjusted.append(
                StatisticData(
                    start=datetime.fromtimestamp(row["start"], tz=timezone.utc),
                    state=per_period,
                    sum=cumsum,
                )
            )
            prev_sum = old_sum

        if adjusted:
            _log("warning", "_adjust_post_range[%s]: adjusting %d rows, first sum=%.4f, last sum=%.4f", statistic_id, len(adjusted), adjusted[0]["sum"], adjusted[-1]["sum"])
            meta = StatisticMetaData(
                mean_type=StatisticMeanType.NONE,
                has_sum=True,
                name=statistic_id,
                source=DOMAIN,
                statistic_id=statistic_id,
                unit_class=EnergyConverter.UNIT_CLASS,
                unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
            )
            async_add_external_statistics(self.hass, meta, adjusted)

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

        stats = await get_instance(self.hass).async_add_executor_job(
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
        prev_sum = 0.0

        for row in rows:
            dt = datetime.fromtimestamp(row["start"], tz=timezone.utc).astimezone(ha_tz)
            # sum is cumulative, extract per-period from difference
            curr_sum = row.get("sum", 0.0) or 0.0
            kwh = curr_sum - prev_sum
            if kwh < 0:
                kwh = 0.0
            prev_sum = curr_sum

            month_key = dt.strftime("%Y-%m")
            cumulative_before = monthly_cumulative.get(month_key, 0.0)
            cost_state = _calculate_cost(kwh, cumulative_before, dt.month, self.rates)
            monthly_cumulative[month_key] = cumulative_before + kwh

            cost_hourly.append(
                StatisticData(start=dt, state=cost_state)
            )
            day_key = dt.strftime("%Y-%m-%d")
            if day_key not in cost_daily_map:
                cost_daily_map[day_key] = 0.0
            cost_daily_map[day_key] += cost_state

        cost_daily: List[StatisticData] = []
        for day_key in sorted(cost_daily_map.keys()):
            dt = _localize(datetime.strptime(day_key, "%Y-%m-%d"))
            cost_daily.append(
                StatisticData(start=dt, state=cost_daily_map[day_key])
            )

        cost_hourly_meta = StatisticMetaData(
            mean_type=StatisticMeanType.NONE,
            has_sum=False,
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
            has_sum=False,
            name=f"Union Power Cost Daily - {self.account_number}",
            source=DOMAIN,
            statistic_id=STAT_COST_DAILY.format(account=self.account_number),
            unit_class=None,
            unit_of_measurement="USD",
        )
        async_add_external_statistics(self.hass, cost_daily_meta, cost_daily)
        _log("warning", "fill_all_stats: created %d daily cost stats", len(cost_daily))

        return len(cost_hourly) + len(cost_daily)

    async def _get_last_stat_with_sum(
        self,
        statistic_id: str,
        query_start: datetime,
    ) -> tuple[Optional[float], Optional[float]]:
        """Get last stat's timestamp and cumulative sum, then find overlap at query_start.

        Returns (last_timestamp, cumulative_sum_at_query_start).
        cumulative_sum_at_query_start is the sum from the row matching query_start,
        or the last stat's sum if no overlap found.
        """
        last_stat = await get_instance(self.hass).async_add_executor_job(
            get_last_statistics,
            self.hass,
            1,
            statistic_id,
            True,
            {"sum"},
        )

        if not last_stat or statistic_id not in last_stat:
            _log("warning", "_get_last_stat_with_sum[%s]: no stats exist yet", statistic_id)
            return (None, None)

        entry = last_stat[statistic_id][0]
        last_ts = entry["start"]
        last_sum = entry.get("sum", 0.0) or 0.0
        _log("warning", "_get_last_stat_with_sum[%s]: last stat ts=%s sum=%.4f", statistic_id, last_ts, last_sum)

        # Query at the start of our API data to find the overlapping row's cumulative sum
        stats = await get_instance(self.hass).async_add_executor_job(
            statistics_during_period,
            self.hass,
            query_start,
            query_start + timedelta(seconds=1),
            {statistic_id},
            "hour",
            None,
            {"sum"},
        )
        rows = stats.get(statistic_id, [])
        if rows:
            overlap_sum = rows[0].get("sum", 0.0) or 0.0
            _log("warning", "_get_last_stat_with_sum[%s]: overlap at %s sum=%.4f", statistic_id, query_start, overlap_sum)
            return (last_ts, overlap_sum)

        _log("warning", "_get_last_stat_with_sum[%s]: no overlap at %s, using last sum=%.4f", statistic_id, query_start, last_sum)
        return (last_ts, last_sum)

    async def _insert_statistics(
        self,
        records: List[IntervalUsage],
        last_cons_sum: float = 0.0,
        last_ret_sum: float = 0.0,
    ) -> None:
        """Insert hourly and daily statistics into the HA recorder.

        state = per-period kWh/cost
        sum = cumulative running total
        """
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
                has_sum=False,
                name=f"Union Power Cost Hourly - {self.account_number}",
                source=DOMAIN,
                statistic_id=STAT_COST_HOURLY.format(account=self.account_number),
                unit_class=None,
                unit_of_measurement="USD",
            )
            cost_daily_meta = StatisticMetaData(
                mean_type=StatisticMeanType.NONE,
                has_sum=False,
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

        # Cumulative sum accumulators (continuing from last known values)
        cons_cumsum = last_cons_sum
        ret_cumsum = last_ret_sum

        # Track cumulative kWh per month for tiered pricing (hourly)
        hourly_cumulative: Dict[str, float] = {}

        # Build hourly consumption stats: state=per-period, sum=cumulative
        cons_hourly_stats: List[StatisticData] = []
        cost_hourly_stats: List[StatisticData] = []
        hourly_cost_by_day: Dict[str, float] = {}
        for rec in hourly_consumption:
            dt = _localize(self.api.parse_timestamp(rec.timestamp))
            month_key = dt.strftime("%Y-%m")
            day_key = rec.timestamp[:10]
            cumulative_before = hourly_cumulative.get(month_key, 0.0)

            cons_cumsum += rec.used_from_grid
            cons_hourly_stats.append(
                StatisticData(start=dt, state=rec.used_from_grid, sum=cons_cumsum)
            )
            if _rates_configured(self.rates):
                cost_state = _calculate_cost(rec.used_from_grid, cumulative_before, dt.month, self.rates)
                cost_hourly_stats.append(
                    StatisticData(start=dt, state=cost_state)
                )
                hourly_cost_by_day[day_key] = hourly_cost_by_day.get(day_key, 0.0) + cost_state
            hourly_cumulative[month_key] = cumulative_before + rec.used_from_grid

        # Build daily consumption stats: state=per-period, sum=cumulative
        cons_daily_stats: List[StatisticData] = []
        cost_daily_stats: List[StatisticData] = []
        cons_daily_cumsum = last_cons_sum
        for day_key in sorted(daily.keys()):
            dt = _localize(datetime.strptime(day_key, "%m/%d/%Y"))
            day_total = daily[day_key]["consumption"]
            cons_daily_cumsum += day_total
            cons_daily_stats.append(
                StatisticData(start=dt, state=day_total, sum=cons_daily_cumsum)
            )
            if _rates_configured(self.rates):
                day_cost = hourly_cost_by_day.get(day_key, 0.0)
                cost_daily_stats.append(
                    StatisticData(start=dt, state=day_cost)
                )

        # Build hourly return stats: state=per-period, sum=cumulative
        ret_hourly_stats: List[StatisticData] = []
        for rec in hourly_return:
            dt = _localize(self.api.parse_timestamp(rec.timestamp))
            ret_cumsum += rec.total_generation
            ret_hourly_stats.append(
                StatisticData(start=dt, state=rec.total_generation, sum=ret_cumsum)
            )

        # Build daily return stats: state=per-period, sum=cumulative
        ret_daily_stats: List[StatisticData] = []
        ret_daily_cumsum = last_ret_sum
        for day_key in sorted(daily.keys()):
            dt = _localize(datetime.strptime(day_key, "%m/%d/%Y"))
            day_total = daily[day_key]["return"]
            ret_daily_cumsum += day_total
            ret_daily_stats.append(
                StatisticData(start=dt, state=day_total, sum=ret_daily_cumsum)
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
                _log("warning", "  cost_hourly: start=%s state=%.4f", s["start"], s["state"])
            async_add_external_statistics(self.hass, cost_hourly_meta, cost_hourly_stats)

        if cost_daily_stats and cost_daily_meta:
            _log("warning", "Inserting %d daily cost stats [%s]:", len(cost_daily_stats), cost_daily_meta["statistic_id"])
            for s in cost_daily_stats[:3]:
                _log("warning", "  cost_daily: start=%s state=%.4f", s["start"], s["state"])
            async_add_external_statistics(self.hass, cost_daily_meta, cost_daily_stats)

        _log("warning", "_insert_statistics complete")

    def _compute_monthly(self, records: List[IntervalUsage], reference_now: datetime) -> tuple[float, str]:
        """Compute current month-to-date total and last reading time."""
        monthly_total = 0.0
        last_reading_time = ""

        for rec in records:
            dt = UnionPowerAPI.parse_timestamp(rec.timestamp)
            if dt.year == reference_now.year and dt.month == reference_now.month:
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
