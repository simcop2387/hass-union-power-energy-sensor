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
    CONF_COST_PER_KWH,
    DOMAIN,
    POLL_INTERVAL_MINUTES,
)

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
        new_data[CONF_COST_PER_KWH] = None
        hass.config_entries.async_update_entry(entry, version=2, data=new_data)
    _log("info", "Migration to version %s complete", entry.version)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Union Power from a config entry."""
    config = entry.data

    api = UnionPowerAPI(
        account_number=config["account_number"],
        password=config["password"],
    )

    # Test connection
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

    # Don't block startup — coordinator returns empty data immediately
    entry.runtime_data = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Schedule initial fetch as background task (does not block startup)
    hass.async_create_task(
        _initial_fetch(hass, coordinator),
        name="union_power_initial_fetch",
    )

    # Schedule daily recurring fetch
    async def _scheduled_fetch(_: datetime) -> None:
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

    # Register import_range service
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

    # Register fill_all_stats service
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
    _log("info", "Starting initial background data fetch...")
    await coordinator.run_fetch_cycle()
    _log("info", "Initial background data fetch complete")


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    if unload_ok:
        coord: UnionPowerDataUpdateCoordinator = entry.runtime_data
        await coord.api.close()

    return unload_ok
