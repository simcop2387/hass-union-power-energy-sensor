"""Union Power Energy integration for Home Assistant."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ConfigEntryError
import homeassistant.helpers.event as evt

from .api import UnionPowerAPI
from .sensor import UnionPowerDataUpdateCoordinator
from .const import (
    CONF_SUMMER_RATE_TIER1,
    CONF_SUMMER_RATE_TIER2,
    CONF_WINTER_RATE_TIER1,
    CONF_WINTER_RATE_TIER2,
    DOMAIN,
    POLL_INTERVAL_MINUTES,
)

# Legacy config key for migration
_CONF_COST_PER_KWH = "cost_per_kwh"

_LOGGER = logging.getLogger(__name__)

def _log(level: str, msg: str, *args) -> None:
    getattr(_LOGGER, level)(msg, *args)

PLATFORMS = [Platform.SENSOR]

SERVICE_IMPORT_RANGE = "import_range"
SERVICE_FILL_ALL_STATS = "fill_all_stats"

IMPORT_RANGE_SCHEMA = vol.Schema(
    {
        vol.Required("start_date"): vol.All(str, vol.Length(min=10, max=10)),
        vol.Required("end_date"): vol.All(str, vol.Length(min=10, max=10)),
    }
)


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate old config entry to new version."""
    _log("info", "Migrating config from version %s", entry.version)
    if entry.version == 1:
        new_data = {**entry.data}
        new_data[CONF_SUMMER_RATE_TIER1] = None
        new_data[CONF_SUMMER_RATE_TIER2] = None
        new_data[CONF_WINTER_RATE_TIER1] = None
        new_data[CONF_WINTER_RATE_TIER2] = None
        hass.config_entries.async_update_entry(entry, version=2, data=new_data)
    if entry.version == 2:
        old_cost = entry.data.get(_CONF_COST_PER_KWH)
        new_data = {**entry.data}
        if old_cost is not None:
            _log("info", "Migrating cost_per_kwh (%s) to seasonal rates", old_cost)
            new_data[CONF_SUMMER_RATE_TIER1] = old_cost
            new_data[CONF_SUMMER_RATE_TIER2] = old_cost
            new_data[CONF_WINTER_RATE_TIER1] = old_cost
            new_data[CONF_WINTER_RATE_TIER2] = old_cost
        else:
            new_data[CONF_SUMMER_RATE_TIER1] = None
            new_data[CONF_SUMMER_RATE_TIER2] = None
            new_data[CONF_WINTER_RATE_TIER1] = None
            new_data[CONF_WINTER_RATE_TIER2] = None
        hass.config_entries.async_update_entry(entry, version=3, data=new_data)
    _log("info", "Migration to version %s complete", entry.version)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Union Power from a config entry."""
    config = entry.data

    api = UnionPowerAPI(
        account_number=config["account_number"],
        password=config["password"],
    )

    try:
        await api.login()
        _log("info", "Successfully connected to Union Power API")
    except Exception as e:
        await api.close()
        raise ConfigEntryError(f"Cannot connect to Union Power: {e}") from e

    coordinator = UnionPowerDataUpdateCoordinator(
        hass=hass,
        api=api,
        update_interval=timedelta(minutes=POLL_INTERVAL_MINUTES),
        config_entry=entry,
    )

    entry.runtime_data = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    _log("warning", "Scheduling initial background fetch and daily 6:00 AM recurring fetch")
    hass.async_create_task(
        _initial_fetch(hass, coordinator),
        name="union_power_initial_fetch",
    )

    async def _scheduled_fetch(_: datetime) -> None:
        _log("warning", "Scheduled 6:00 AM fetch triggered")
        await coordinator.run_fetch_cycle()

    entry.async_on_unload(
        evt.async_track_time_change(
            hass,
            _scheduled_fetch,
            hour=6,
            minute=0,
            second=0,
        )
    )

    async def handle_import_range(call: ServiceCall) -> None:
        """Handle the import_range service call."""
        start = datetime.strptime(call.data["start_date"], "%Y-%m-%d")
        end = datetime.strptime(call.data["end_date"], "%Y-%m-%d")
        _log("warning", "Service import_range called: %s → %s", start.date(), end.date())
        coord: UnionPowerDataUpdateCoordinator = entry.runtime_data
        count = await coord.import_range(start, end)
        _log("warning", "Service import_range done: %d records", count)

    hass.services.async_register(
        DOMAIN,
        SERVICE_IMPORT_RANGE,
        handle_import_range,
        schema=IMPORT_RANGE_SCHEMA,
    )

    entry.async_on_unload(
        lambda: hass.services.async_remove(DOMAIN, SERVICE_IMPORT_RANGE)
    )

    async def handle_fill_all_stats(call: ServiceCall) -> None:
        """Handle the fill_all_stats service call."""
        _log("warning", "Service fill_all_stats called")
        coord: UnionPowerDataUpdateCoordinator = entry.runtime_data
        count = await coord.fill_all_stats()
        _log("warning", "Service fill_all_stats done: %d records updated", count)

    hass.services.async_register(
        DOMAIN,
        SERVICE_FILL_ALL_STATS,
        handle_fill_all_stats,
    )

    entry.async_on_unload(
        lambda: hass.services.async_remove(DOMAIN, SERVICE_FILL_ALL_STATS)
    )

    return True


async def _initial_fetch(hass: HomeAssistant, coordinator: UnionPowerDataUpdateCoordinator) -> None:
    """Run the initial data fetch in the background."""
    _log("warning", "Starting initial background data fetch...")
    try:
        await coordinator.run_fetch_cycle()
        _log("warning", "Initial background data fetch complete")
    except Exception:
        _log("exception", "Initial background data fetch failed")


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    if unload_ok:
        coord: UnionPowerDataUpdateCoordinator = entry.runtime_data
        await coord.api.close()

    return unload_ok
