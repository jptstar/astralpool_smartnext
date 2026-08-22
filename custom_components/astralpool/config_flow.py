"""Config flow for AstralPool devices."""

from __future__ import annotations

import asyncio
import logging

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.config_entries import SOURCE_RECONFIGURE, ConfigFlowResult
from homeassistant.const import CONF_HOST, CONF_PORT, CONF_TIMEOUT, UnitOfTemperature
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
)

from .const import (
    CONF_DEVICE_TYPE,
    CONF_RECONNECT_DELAY,
    CONF_SCAN_INTERVAL,
    CONF_UNIT_ID,
    DEFAULT_PORT,
    DEFAULT_RECONNECT_DELAY,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_TIMEOUT,
    DEFAULT_UNIT_IDS,
    DEVICE_NAMES,
    DEVICE_TYPE_ELYO_TOUCH,
    DEVICE_TYPE_SMARTNEXT,
    DOMAIN,
    MAX_SCAN_INTERVAL,
    MIN_SCAN_INTERVAL,
)
from .devices.elyo_touch.api import ElyoTouchApi, ElyoTouchCommunicationError
from .devices.smartnext.api import SmartNextApi, SmartNextCommunicationError
from .devices.smartnext.guided_options import SmartNextGuidedCalibrationOptionsMixin
from .devices.smartnext.maintenance import (
    ACTION_RESTART_DEVICE,
    WATCHDOG_RESTART_SECONDS,
    SmartNextMaintenanceError,
    async_arm_restart_watchdog,
    async_read_watchdog,
    async_restore_watchdog,
)
from .devices.smartnext.temperature_calibration import (
    async_calibrate_temperature,
    async_reset_temperature_calibration,
)

_LOGGER = logging.getLogger(__name__)

_UNIT_ID_SELECTOR = vol.All(
    NumberSelector(
        NumberSelectorConfig(
            min=0,
            max=247,
            step=1,
            mode=NumberSelectorMode.BOX,
        )
    ),
    vol.Coerce(int),
)

_TEMPERATURE_SELECTOR = NumberSelector(
    NumberSelectorConfig(
        min=0,
        max=60,
        step=0.1,
        unit_of_measurement=UnitOfTemperature.CELSIUS,
        mode=NumberSelectorMode.BOX,
    )
)

ACTION_CALIBRATE_TEMPERATURE = "calibrate_temperature"
ACTION_RESTORE_TEMPERATURE_CALIBRATION = "restore_temperature_calibration"


def _connection_schema(device_type: str, defaults: dict | None = None) -> vol.Schema:
    defaults = defaults or {}
    return vol.Schema(
        {
            vol.Required(CONF_HOST, default=defaults.get(CONF_HOST, "")): str,
            vol.Required(
                CONF_PORT, default=defaults.get(CONF_PORT, DEFAULT_PORT)
            ): vol.All(vol.Coerce(int), vol.Range(min=1, max=65535)),
            vol.Required(
                CONF_UNIT_ID,
                default=defaults.get(CONF_UNIT_ID, DEFAULT_UNIT_IDS[device_type]),
            ): _UNIT_ID_SELECTOR,
            vol.Required(
                CONF_TIMEOUT, default=defaults.get(CONF_TIMEOUT, DEFAULT_TIMEOUT)
            ): vol.All(vol.Coerce(float), vol.Range(min=0.5, max=60)),
            vol.Required(
                CONF_RECONNECT_DELAY, default=defaults.get(CONF_RECONNECT_DELAY, DEFAULT_RECONNECT_DELAY),
            ): vol.All(vol.Coerce(float), vol.Range(min=0, max=300)),
            vol.Required(
                CONF_SCAN_INTERVAL, default=defaults.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
            ): vol.All(
                vol.Coerce(int),
                vol.Range(min=MIN_SCAN_INTERVAL, max=MAX_SCAN_INTERVAL),
            ),
        }
    )


def _connection_unique_id(device_type: str, data: dict) -> str:
    """Return the unique ID for one AstralPool Modbus endpoint."""
    return (
        f"{device_type}:"
        f"{data[CONF_HOST]}:"
        f"{data[CONF_PORT]}:"
        f"{data[CONF_UNIT_ID]}"
    )


async def _test_connection(device_type: str, data: dict) -> None:
    api_class = SmartNextApi if device_type == DEVICE_TYPE_SMARTNEXT else ElyoTouchApi
    api = api_class(
        host=data[CONF_HOST],
        port=data[CONF_PORT],
        timeout=data[CONF_TIMEOUT],
        reconnect_delay=data[CONF_RECONNECT_DELAY],
        unit_id=data[CONF_UNIT_ID],
    )
    try:
        await api.async_connect()
        await api.async_read_all()
    finally:
        await api.async_close()


class AstralPoolConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle an AstralPool config flow."""

    VERSION = 1

    def __init__(self) -> None:
        self._device_type: str | None = None

    async def async_step_user(self, user_input=None) -> ConfigFlowResult:
        """Select the AstralPool device family."""
        if user_input is not None:
            self._device_type = user_input[CONF_DEVICE_TYPE]
            return await self.async_step_connection()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_DEVICE_TYPE): vol.In(
                        {
                            DEVICE_TYPE_SMARTNEXT: DEVICE_NAMES[DEVICE_TYPE_SMARTNEXT],
                            DEVICE_TYPE_ELYO_TOUCH: DEVICE_NAMES[DEVICE_TYPE_ELYO_TOUCH],
                        }
                    )
                }
            ),
        )

    async def async_step_connection(self, user_input=None) -> ConfigFlowResult:
        """Configure and validate the selected device."""
        if self._device_type is None:
            return await self.async_step_user()

        reconfigure_entry = (
            self._get_reconfigure_entry()
            if self.source == SOURCE_RECONFIGURE
            else None
        )
        errors: dict[str, str] = {}

        if user_input is not None:
            data = {CONF_DEVICE_TYPE: self._device_type, **user_input}
            unique_id = _connection_unique_id(self._device_type, user_input)

            if reconfigure_entry is not None:
                existing = self.hass.config_entries.async_entry_for_domain_unique_id(
                    DOMAIN, unique_id
                )
                if existing is not None and existing.entry_id != reconfigure_entry.entry_id:
                    return self.async_abort(reason="already_configured")
            else:
                await self.async_set_unique_id(unique_id)
                self._abort_if_unique_id_configured()

            try:
                await _test_connection(self._device_type, user_input)
            except (
                SmartNextCommunicationError,
                ElyoTouchCommunicationError,
                OSError,
                TimeoutError,
            ):
                errors["base"] = "cannot_connect"
            except Exception:  # noqa: BLE001
                errors["base"] = "unknown"
            else:
                title = f"{DEVICE_NAMES[self._device_type]} {user_input[CONF_HOST]}"
                if reconfigure_entry is not None:
                    return self.async_update_reload_and_abort(
                        reconfigure_entry,
                        data=data,
                        options={},
                        title=title,
                        unique_id=unique_id,
                    )
                return self.async_create_entry(title=title, data=data)

        defaults = user_input
        if reconfigure_entry is not None and defaults is None:
            defaults = {
                CONF_HOST: reconfigure_entry.data[CONF_HOST],
                CONF_PORT: reconfigure_entry.data[CONF_PORT],
                CONF_UNIT_ID: reconfigure_entry.options.get(
                    CONF_UNIT_ID, reconfigure_entry.data[CONF_UNIT_ID]
                ),
                CONF_TIMEOUT: reconfigure_entry.options.get(
                    CONF_TIMEOUT, reconfigure_entry.data[CONF_TIMEOUT]
                ),
                CONF_RECONNECT_DELAY: reconfigure_entry.options.get(
                    CONF_RECONNECT_DELAY,
                    reconfigure_entry.data[CONF_RECONNECT_DELAY],
                ),
                CONF_SCAN_INTERVAL: reconfigure_entry.options.get(
                    CONF_SCAN_INTERVAL,
                    reconfigure_entry.data[CONF_SCAN_INTERVAL],
                ),
            }

        return self.async_show_form(
            step_id="connection",
            data_schema=_connection_schema(self._device_type, defaults),
            errors=errors,
            description_placeholders={"device_name": DEVICE_NAMES[self._device_type]},
        )

    async def async_step_reconfigure(self, user_input=None) -> ConfigFlowResult:
        """Reconfigure the Modbus endpoint for an existing AstralPool device."""
        entry = self._get_reconfigure_entry()
        self._device_type = entry.data[CONF_DEVICE_TYPE]
        return await self.async_step_connection(user_input)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        """Return the options flow."""
        return AstralPoolOptionsFlow(config_entry)


class AstralPoolOptionsFlow(
    SmartNextGuidedCalibrationOptionsMixin,
    config_entries.OptionsFlow,
):
    """Handle AstralPool communication and Smart Next maintenance options."""

    def __init__(self, config_entry) -> None:
        self._config_entry = config_entry
        self._maintenance_action: str | None = None

    async def async_step_init(self, user_input=None) -> ConfigFlowResult:
        """Open the options menu."""
        if self._config_entry.data[CONF_DEVICE_TYPE] != DEVICE_TYPE_SMARTNEXT:
            return await self.async_step_communication(user_input)

        return self.async_show_menu(
            step_id="init",
            menu_options=["communication", "maintenance"],
        )

    async def async_step_communication(self, user_input=None) -> ConfigFlowResult:
        """Manage runtime-tunable Modbus communication options."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        defaults = {
            CONF_UNIT_ID: self._config_entry.options.get(
                CONF_UNIT_ID, self._config_entry.data[CONF_UNIT_ID]
            ),
            CONF_TIMEOUT: self._config_entry.options.get(
                CONF_TIMEOUT, self._config_entry.data[CONF_TIMEOUT]
            ),
            CONF_RECONNECT_DELAY: self._config_entry.options.get(
                CONF_RECONNECT_DELAY,
                self._config_entry.data[CONF_RECONNECT_DELAY],
            ),
            CONF_SCAN_INTERVAL: self._config_entry.options.get(
                CONF_SCAN_INTERVAL,
                self._config_entry.data[CONF_SCAN_INTERVAL],
            ),
        }

        return self.async_show_form(
            step_id="communication",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_UNIT_ID, default=defaults[CONF_UNIT_ID]
                    ): _UNIT_ID_SELECTOR,
                    vol.Required(
                        CONF_TIMEOUT, default=defaults[CONF_TIMEOUT]
                    ): vol.All(vol.Coerce(float), vol.Range(min=0.5, max=60)),
                    vol.Required(
                        CONF_RECONNECT_DELAY,
                        default=defaults[CONF_RECONNECT_DELAY],
                    ): vol.All(vol.Coerce(float), vol.Range(min=0, max=300)),
                    vol.Required(
                        CONF_SCAN_INTERVAL,
                        default=defaults[CONF_SCAN_INTERVAL],
                    ): vol.All(
                        vol.Coerce(int),
                        vol.Range(min=MIN_SCAN_INTERVAL, max=MAX_SCAN_INTERVAL),
                    ),
                }
            ),
        )

    def _temperature_available(self) -> bool:
        """Return whether the Smart Next has temperature measurement hardware."""
        return bool(
            self._config_entry.runtime_data.data.get(
                "technology_temperature_implemented", False
            )
        )

    async def async_step_maintenance(self, user_input=None) -> ConfigFlowResult:
        """Show the three Smart Next maintenance families."""
        return self.async_show_menu(
            step_id="maintenance",
            menu_options={
                "restart_device": "Redémarrer le Smart Next",
                "calibrate_sensor": "Calibrer un capteur",
                "restore_calibration": "Restaurer la calibration d’usine",
            },
        )

    async def async_step_restart_device(self, user_input=None) -> ConfigFlowResult:
        """Open restart confirmation."""
        self._maintenance_action = ACTION_RESTART_DEVICE
        return await self.async_step_maintenance_confirm(user_input)

    async def async_step_restore_calibration(self, user_input=None) -> ConfigFlowResult:
        """Choose a sensor whose calibration should return to factory defaults."""
        menu: dict[str, str] = {}
        if self._ph_available():
            menu["restore_ph_calibration"] = "pH"
        if self._orp_available():
            menu["restore_orp_calibration"] = "Redox / ORP"
        if self._temperature_available():
            menu["restore_temperature_calibration"] = "Température"
        if not menu:
            return self.async_abort(reason="maintenance_unsupported")
        return self.async_show_menu(step_id="restore_calibration", menu_options=menu)

    async def async_step_calibrate_temperature(self, user_input=None) -> ConfigFlowResult:
        """Calibrate temperature using the validated 0x22 -> 0xB0F sequence."""
        if not self._temperature_available():
            return self.async_abort(reason="maintenance_unsupported")

        if user_input is not None:
            reference_temperature = float(user_input["temperature"])
            try:
                await async_calibrate_temperature(
                    self._config_entry.runtime_data.api,
                    reference_temperature,
                )
                await self._config_entry.runtime_data.async_request_refresh()
            except (SmartNextCommunicationError, OSError, TimeoutError):
                return self.async_abort(reason="maintenance_communication_failed")
            return self.async_abort(reason="maintenance_ok")

        current = self._config_entry.runtime_data.data.get("temperature")
        default_temperature = (
            float(current)
            if isinstance(current, (int, float)) and 0 <= float(current) <= 60
            else 25.0
        )
        return self.async_show_form(
            step_id="calibrate_temperature",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        "temperature", default=default_temperature
                    ): _TEMPERATURE_SELECTOR
                }
            ),
        )

    async def async_step_restore_temperature_calibration(
        self, user_input=None
    ) -> ConfigFlowResult:
        """Confirm restoration of the factory temperature calibration."""
        self._maintenance_action = ACTION_RESTORE_TEMPERATURE_CALIBRATION
        return await self.async_step_maintenance_confirm(user_input)

    async def async_step_maintenance_confirm(self, user_input=None) -> ConfigFlowResult:
        """Require explicit confirmation before restart or factory reset."""
        if self._maintenance_action is None:
            return await self.async_step_maintenance()

        errors: dict[str, str] = {}
        if user_input is not None:
            if not user_input.get("confirm", False):
                errors["base"] = "confirmation_required"
            elif self._maintenance_action == ACTION_RESTART_DEVICE:
                self.hass.async_create_background_task(
                    self._async_restart_smartnext_background(),
                    f"{DOMAIN}: Smart Next restart",
                )
                return self.async_abort(reason="restart_started")
            elif self._maintenance_action == ACTION_RESTORE_TEMPERATURE_CALIBRATION:
                try:
                    await async_reset_temperature_calibration(
                        self._config_entry.runtime_data.api
                    )
                    await self._config_entry.runtime_data.async_request_refresh()
                except (SmartNextCommunicationError, OSError, TimeoutError):
                    return self.async_abort(reason="maintenance_communication_failed")
                return self.async_abort(reason="maintenance_ok")
            else:
                return self.async_abort(reason="maintenance_unsupported")

        return self.async_show_form(
            step_id="maintenance_confirm",
            data_schema=vol.Schema({vol.Required("confirm", default=False): bool}),
            errors=errors,
        )

    def _new_smartnext_api(self) -> SmartNextApi:
        """Build a standalone client using the active entry settings."""
        entry = self._config_entry
        return SmartNextApi(
            host=entry.data[CONF_HOST],
            port=entry.data[CONF_PORT],
            timeout=float(entry.options.get(CONF_TIMEOUT, entry.data[CONF_TIMEOUT])),
            reconnect_delay=float(
                entry.options.get(
                    CONF_RECONNECT_DELAY,
                    entry.data[CONF_RECONNECT_DELAY],
                )
            ),
            unit_id=int(entry.options.get(CONF_UNIT_ID, entry.data[CONF_UNIT_ID])),
        )

    async def _async_restart_smartnext_background(self) -> None:
        """Run the restart after the options flow has closed."""
        await asyncio.sleep(1)
        try:
            await self._async_restart_smartnext()
        except SmartNextMaintenanceError as err:
            _LOGGER.error("Smart Next restart failed: %s", err.reason)
        except (SmartNextCommunicationError, OSError, TimeoutError) as err:
            _LOGGER.error("Smart Next restart communication failure: %s", err)
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Unexpected Smart Next restart failure")

    async def _async_restart_smartnext(self) -> str:
        """Perform a one-shot restart through the documented Modbus watchdog."""
        entry = self._config_entry

        _, watchdog_config = await async_read_watchdog(entry.runtime_data.api)
        if watchdog_config != 1:
            raise SmartNextMaintenanceError("watchdog_not_restart")

        if not await self.hass.config_entries.async_unload(entry.entry_id):
            raise SmartNextMaintenanceError("restart_unload_failed")

        previous_timeout: int | None = None
        arm_api = self._new_smartnext_api()
        try:
            try:
                await arm_api.async_connect()
                previous_timeout = await async_arm_restart_watchdog(arm_api)
            except (SmartNextCommunicationError, OSError, TimeoutError):
                await self.hass.config_entries.async_reload(entry.entry_id)
                raise SmartNextMaintenanceError("restart_arm_failed") from None
        finally:
            await arm_api.async_close()

        await asyncio.sleep(WATCHDOG_RESTART_SECONDS + 10)

        restored = False
        restore_api = self._new_smartnext_api()
        try:
            for _ in range(12):
                try:
                    await restore_api.async_connect()
                    await async_restore_watchdog(restore_api, previous_timeout)
                    restored = True
                    break
                except (SmartNextCommunicationError, OSError, TimeoutError):
                    await restore_api.async_close()
                    await asyncio.sleep(5)
        finally:
            await restore_api.async_close()

        await self.hass.config_entries.async_reload(entry.entry_id)

        if not restored:
            raise SmartNextMaintenanceError("restart_restore_failed")
        return "restart_ok"
