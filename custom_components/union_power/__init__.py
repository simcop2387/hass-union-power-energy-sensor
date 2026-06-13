"""Union Power Energy integration for Home Assistant."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ConfigEntryError

from .api import UnionPowerAPI
from .sensor import UnionPowerDataUpdateCoordinator
from .const import (
    DOMAIN,
    POLL_INTERVAL_MINUTES,
)

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.SENSOR]

SERVICE_IMPORT_RANGE = "import_range"

IMPORT_RANGE_SCHEMA = vol.Schema(
    {
        vol.Required("start_date"): vol.All(str, vol.Length(min=10, max=10)),
        vol.Required("end_date"): vol.All(str, vol.Length(min=10, max=10)),
    }
)


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
        _LOGGER.info("Successfully connected to Union Power API")
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
        hass.helpers.event.async_track_time_change(
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

        coord: UnionPowerDataUpdateCoordinator = entry.runtime_data
        count = await coord.import_range(start, end)
        _LOGGER.info("Service import_range: imported %d records", count)

    hass.services.async_register(
        DOMAIN,
        SERVICE_IMPORT_RANGE,
        handle_import_range,
        schema=IMPORT_RANGE_SCHEMA,
    )

    entry.async_on_unload(
        lambda: hass.services.async_remove(DOMAIN, SERVICE_IMPORT_RANGE)
    )

    return True


async def _initial_fetch(hass: HomeAssistant, coordinator: UnionPowerDataUpdateCoordinator) -> None:
    """Run the initial data fetch in the background."""
    _LOGGER.info("Starting initial background data fetch...")
    await coordinator.run_fetch_cycle()
    _LOGGER.info("Initial background data fetch complete")


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    if unload_ok:
        coord: UnionPowerDataUpdateCoordinator = entry.runtime_data
        await coord.api.close()

    return unload_ok
