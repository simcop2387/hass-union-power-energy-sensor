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
    CONF_COST_PER_KWH,
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

    async def _async_update_data(self) -> Dict[str, Any]:
        """Return stored data. Fetching is done by background tasks, not here."""
        return self.data or {}

    async def run_fetch_cycle(self) -> None:
        """Run a full fetch cycle: login, fetch, insert stats, update coordinator data.

        Called by background tasks, NOT by the coordinator's update loop.
        """
        try:
            _log("info", "Running fetch cycle")
            await self.api.login()

            now = datetime.now()
            end_date = (now - timedelta(days=DATA_LAG_DAYS)).replace(
                hour=0, minute=0, second=0, microsecond=0
            )

            last_stat = await self._get_last_stat(
                STAT_CONSUMPTION_HOURLY.format(account=self.account_number)
            )

            if last_stat is None:
                start_date = end_date - timedelta(days=HISTORICAL_IMPORT_DAYS)
                _log("info", "Initial import: %s → %s (%d days)", start_date.date(), end_date.date(), HISTORICAL_IMPORT_DAYS)
            else:
                start_date = datetime.fromtimestamp(last_stat, tz=timezone.utc).replace(
                    tzinfo=None
                ) - timedelta(days=2)
                if start_date >= end_date:
                    _log("debug", "No new data to fetch")
                    return
                _log("info", "Incremental update: %s → %s", start_date.date(), end_date.date())

            records = await self.api.get_interval_usage(start_date, end_date)

            if not records:
                _log("warning", "No interval data returned for %s → %s", start_date.date(), end_date.date())
                return

            cost_per_kwh = self.config_entry.data.get(CONF_COST_PER_KWH)
            await self._insert_statistics(records, cost_per_kwh=cost_per_kwh)

            monthly_total, last_reading_time = self._compute_monthly(records)

            self.data = {
                ENERGY_SENSOR_KEY: monthly_total,
                ATTR_LAST_READING_TIME: last_reading_time,
                ATTR_ACCOUNT_NUMBER: self.account_number,
            }
            _log("info", "Fetch cycle complete: %d records, %.3f kWh monthly", len(records), monthly_total)

        except (UnionPowerAuthenticationError, UnionPowerConnectionError) as e:
            _log("error", "Fetch cycle failed - authentication/connection: %s", e)
        except UnionPowerError as e:
            _log("error", "Fetch cycle failed - API error: %s", e)
        except Exception as e:
            _log("exception", "Fetch cycle failed - unexpected error: %s", e)

    async def import_range(
        self, start_date: datetime, end_date: datetime
    ) -> int:
        """Manually import data for a custom date range.

        Returns number of records imported.
        """
        _log("warning", "import_range starting: %s → %s", start_date.date(), end_date.date())
        await self.api.login()
        _log("warning", "import_range login OK, fetching data...")
        records = await self.api.get_interval_usage(start_date, end_date)
        _log("warning", "import_range got %d records", len(records))

        if not records:
            _log("warning", "No data returned for range %s → %s", start_date.date(), end_date.date())
            return 0

        cost_per_kwh = self.config_entry.data.get(CONF_COST_PER_KWH)
        await self._insert_statistics(records, cost_per_kwh=cost_per_kwh)
        _log("warning", "import_range inserted %d records for %s → %s", len(records), start_date.date(), end_date.date())
        return len(records)

    async def fill_all_stats(self) -> int:
        """Create cost statistics from all existing consumption data.

        Reads all existing hourly consumption stats from the recorder and
        creates matching hourly/daily cost stats if cost_per_kwh is configured.
        Returns number of cost records created.
        """
        cost_per_kwh = self.config_entry.data.get(CONF_COST_PER_KWH)
        if not cost_per_kwh:
            _log("warning", "fill_all_stats: cost_per_kwh not configured (value=%s), nothing to do", cost_per_kwh)
            return 0

        _log("warning", "fill_all_stats: cost_per_kwh = %s", cost_per_kwh)

        stat_id = STAT_CONSUMPTION_HOURLY.format(account=self.account_number)

        # Use a far-past start date — get_start_time() only returns current session start
        start_time = datetime(2020, 1, 1, tzinfo=timezone.utc)
        end_time = datetime.now(tz=timezone.utc)

        _log("warning", "fill_all_stats: reading stats from %s to %s", start_time, end_time)

        # Read all existing hourly stats
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

        # Build hourly cost stats from consumption data (cumulative)
        ha_tz = ZoneInfo(self.hass.config.time_zone)
        def _localize(dt: datetime) -> datetime:
            if dt.tzinfo is None:
                return dt.replace(tzinfo=ha_tz)
            return dt
        cost_hourly: List[StatisticData] = []
        cost_daily_map: Dict[str, float] = {}
        cost_sum = 0.0

        for row in rows:
            dt = datetime.fromtimestamp(row["start"], tz=timezone.utc).astimezone(ha_tz)
            s = row.get("sum", 0.0) or 0.0
            state = row.get("state", s) or 0.0
            cost_state = state * cost_per_kwh
            cost_sum += cost_state
            cost_hourly.append(
                StatisticData(start=dt, state=cost_state, sum=round(cost_sum, 6))
            )
            # Accumulate daily cost
            day_key = dt.strftime("%Y-%m-%d")
            if day_key not in cost_daily_map:
                cost_daily_map[day_key] = 0.0
            cost_daily_map[day_key] += cost_state

        # Build daily cost stats (cumulative)
        cost_daily: List[StatisticData] = []
        daily_cost_sum = 0.0
        for day_key in sorted(cost_daily_map.keys()):
            dt = _localize(datetime.strptime(day_key, "%Y-%m-%d"))
            daily_cost_sum += cost_daily_map[day_key]
            cost_daily.append(
                StatisticData(start=dt, state=cost_daily_map[day_key], sum=round(daily_cost_sum, 6))
            )

        # Insert hourly cost stats
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

        # Insert daily cost stats
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

    async def _get_last_stat(self, statistic_id: str) -> Optional[float]:
        """Get the timestamp of the last statistic entry."""
        last_stat = await self.hass.async_add_executor_job(
            get_last_statistics, self.hass, 1, statistic_id, True, set()
        )
        if last_stat and statistic_id in last_stat:
            return last_stat[statistic_id][0].get("start")
        return None

    async def _insert_statistics(self, records: List[IntervalUsage], cost_per_kwh: Optional[float] = None) -> None:
        """Insert hourly and daily statistics into the HA recorder."""
        # Group by day
        daily: Dict[str, Dict[str, float]] = {}
        hourly_consumption: List[IntervalUsage] = []
        hourly_return: List[IntervalUsage] = []

        for rec in records:
            day_key = rec.timestamp[:10]  # "MM/DD/YYYY"
            if day_key not in daily:
                daily[day_key] = {"consumption": 0.0, "return": 0.0}
            daily[day_key]["consumption"] += rec.used_from_grid
            daily[day_key]["return"] += rec.total_generation

            hourly_consumption.append(rec)
            if rec.total_generation > 0:
                hourly_return.append(rec)

        # Build stat metadata
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

        # Separate cost stats (if cost_per_kwh is configured)
        cost_hourly_meta = None
        cost_daily_meta = None
        if cost_per_kwh:
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

        # Localize naive datetimes to HA timezone
        ha_tz = ZoneInfo(self.hass.config.time_zone)

        def _localize(dt: datetime) -> datetime:
            if dt.tzinfo is None:
                return dt.replace(tzinfo=ha_tz)
            return dt

        # Build hourly consumption stats (cumulative)
        cons_sum = 0.0
        cons_hourly_stats: List[StatisticData] = []
        cost_hourly_stats: List[StatisticData] = []
        cost_h_sum = 0.0
        for rec in hourly_consumption:
            dt = _localize(self.api.parse_timestamp(rec.timestamp))
            cons_sum += rec.used_from_grid
            cons_hourly_stats.append(
                StatisticData(start=dt, state=rec.used_from_grid, sum=cons_sum)
            )
            if cost_per_kwh:
                cost_state = rec.used_from_grid * cost_per_kwh
                cost_h_sum += cost_state
                cost_hourly_stats.append(
                    StatisticData(start=dt, state=cost_state, sum=round(cost_h_sum, 6))
                )

        # Build daily consumption stats (cumulative)
        cons_daily_sum = 0.0
        cons_daily_stats: List[StatisticData] = []
        cost_daily_stats: List[StatisticData] = []
        cost_d_sum = 0.0
        for day_key in sorted(daily.keys()):
            dt = _localize(datetime.strptime(day_key, "%m/%d/%Y"))
            day_total = daily[day_key]["consumption"]
            cons_daily_sum += day_total
            cons_daily_stats.append(
                StatisticData(start=dt, state=day_total, sum=cons_daily_sum)
            )
            if cost_per_kwh:
                cost_state = day_total * cost_per_kwh
                cost_d_sum += cost_state
                cost_daily_stats.append(
                    StatisticData(start=dt, state=cost_state, sum=round(cost_d_sum, 6))
                )

        # Build hourly return stats
        ret_sum = 0.0
        ret_hourly_stats: List[StatisticData] = []
        for rec in hourly_return:
            dt = _localize(self.api.parse_timestamp(rec.timestamp))
            ret_sum += rec.total_generation
            ret_hourly_stats.append(
                StatisticData(start=dt, state=rec.total_generation, sum=ret_sum)
            )

        # Build daily return stats
        ret_daily_sum = 0.0
        ret_daily_stats: List[StatisticData] = []
        for day_key in sorted(daily.keys()):
            dt = _localize(datetime.strptime(day_key, "%m/%d/%Y"))
            day_total = daily[day_key]["return"]
            ret_daily_sum += day_total
            ret_daily_stats.append(
                StatisticData(start=dt, state=day_total, sum=ret_daily_sum)
            )

        # Insert into recorder
        if cons_hourly_stats:
            _log("info", "Adding %d hourly consumption statistics", len(cons_hourly_stats))
            async_add_external_statistics(self.hass, cons_hourly_meta, cons_hourly_stats)

        if cons_daily_stats:
            _log("info", "Adding %d daily consumption statistics", len(cons_daily_stats))
            async_add_external_statistics(self.hass, cons_daily_meta, cons_daily_stats)

        if ret_hourly_stats:
            _log("info", "Adding %d hourly return statistics", len(ret_hourly_stats))
            async_add_external_statistics(self.hass, ret_hourly_meta, ret_hourly_stats)

        if ret_daily_stats:
            _log("info", "Adding %d daily return statistics", len(ret_daily_stats))
            async_add_external_statistics(self.hass, ret_daily_meta, ret_daily_stats)

        if cost_hourly_stats and cost_hourly_meta:
            _log("info", "Adding %d hourly cost statistics", len(cost_hourly_stats))
            async_add_external_statistics(self.hass, cost_hourly_meta, cost_hourly_stats)

        if cost_daily_stats and cost_daily_meta:
            _log("info", "Adding %d daily cost statistics", len(cost_daily_stats))
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
