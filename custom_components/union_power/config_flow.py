"""Config flow for Union Power integration."""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResult

from .const import (
    DOMAIN,
    CONF_ACCOUNT_NUMBER,
    CONF_PASSWORD,
)
from .api import UnionPowerAPI
from .exceptions import UnionPowerAuthenticationError, UnionPowerConnectionError

_LOGGER = logging.getLogger(__name__)


class UnionPowerConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Union Power."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the initial step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                await self._validate_input(user_input)
            except UnionPowerAuthenticationError:
                errors["base"] = "invalid_auth"
            except UnionPowerConnectionError:
                errors["base"] = "cannot_connect"
            except Exception:  # pylint: disable=broad-except
                _LOGGER.exception("Unexpected exception")
                errors["base"] = "unknown"
            else:
                if self.source == config_entries.SOURCE_RECONFIGURE:
                    return self.async_update_reload_and_abort(
                        self._get_reconfigure_entry(), data_updates=user_input
                    )
                return self.async_create_entry(
                    title=f"Union Power ({user_input[CONF_ACCOUNT_NUMBER]})",
                    data=user_input,
                )

        schema = vol.Schema(
            {
                vol.Required(CONF_ACCOUNT_NUMBER): str,
                vol.Required(CONF_PASSWORD): str,
            }
        )

        return self.async_show_form(
            step_id="user",
            data_schema=schema,
            errors=errors,
        )

    async def _validate_input(self, data: dict[str, Any]) -> None:
        """Validate that the user input allows us to connect."""
        api = UnionPowerAPI(
            account_number=data[CONF_ACCOUNT_NUMBER],
            password=data[CONF_PASSWORD],
        )
        try:
            await api.login()
        finally:
            await api.close()

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle a reconfiguration flow."""
        return await self.async_step_user(user_input)
