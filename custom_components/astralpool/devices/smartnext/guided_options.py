"""Guided Home Assistant options-flow steps for Smart Next calibration."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import voluptuous as vol

from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
)

from .api import SmartNextCommunicationError
from .guided_calibration import (
    RESPONSE_E2,
    RESPONSE_E3,
    RESPONSE_FIRST_POINT_OK,
    RESPONSE_INITIALIZING,
    RESPONSE_MODE_LOST,
    RESPONSE_NONE,
    RESPONSE_OK,
    RESPONSE_UNAVAILABLE,
    CalibrationSavedState,
    GuidedCalibrationError,
    async_begin_bypassed_calibration,
    async_calibrate_ph_fast,
    async_prepare_bypassed_calibration,
    async_rearm_calibration_mode,
    async_reset_orp_calibration,
    async_reset_ph_calibration,
    async_restart_orp_after_error,
    async_restart_standard_ph_after_error,
    async_restore_bypassed_calibration,
    async_trigger_orp_470,
    async_trigger_ph4,
    async_trigger_ph7,
)

_PH_SELECTOR = NumberSelector(
    NumberSelectorConfig(
        min=0,
        max=12,
        step=0.01,
        mode=NumberSelectorMode.BOX,
    )
)


class SmartNextGuidedCalibrationOptionsMixin:
    """Add hardware-validated pH and ORP calibration assistants."""

    _ph_saved_state: CalibrationSavedState | None = None
    _orp_saved_state: CalibrationSavedState | None = None
    _ph_last_error: int | str | None = None
    _orp_last_error: int | str | None = None

    def _ph_available(self) -> bool:
        return bool(
            self._config_entry.runtime_data.data.get("technology_ph_implemented", False)
        )

    def _orp_available(self) -> bool:
        return bool(
            self._config_entry.runtime_data.data.get("technology_orp_implemented", False)
        )

    @staticmethod
    def _response_error_key(response: int) -> str:
        return {
            RESPONSE_E2: "calibration_e2",
            RESPONSE_E3: "calibration_e3",
            RESPONSE_UNAVAILABLE: "calibration_unavailable",
            RESPONSE_INITIALIZING: "calibration_initializing",
            RESPONSE_MODE_LOST: "calibration_mode_lost",
            RESPONSE_NONE: "calibration_timeout",
        }.get(response, "calibration_failed")

    @staticmethod
    def _response_text(response: int | str | None) -> str:
        if response == RESPONSE_E2:
            return "E2 — IR 0x22 = 2"
        if response == RESPONSE_E3:
            return "E3 — IR 0x22 = 3"
        if response == RESPONSE_UNAVAILABLE:
            return "Calibration unavailable — IR 0x22 = 4"
        if response == RESPONSE_INITIALIZING:
            return "Sensor initializing — IR 0x22 = 5"
        if response == RESPONSE_MODE_LOST:
            return "Calibration mode ended without a usable response"
        if response == RESPONSE_NONE:
            return "No calibration response before timeout"
        if response == "communication":
            return "Modbus communication interrupted"
        if isinstance(response, str):
            return response
        return f"Unexpected IR 0x22 response: {response}"

    async def _async_confirmation_step(
        self,
        *,
        step_id: str,
        field: str,
        user_input,
        next_step: Callable[[], Awaitable],
    ):
        """Render one explicit manual confirmation before moving to the next step."""
        errors: dict[str, str] = {}
        if user_input is not None:
            if not user_input.get(field, False):
                errors["base"] = "confirmation_required"
            else:
                return await next_step()
        return self.async_show_form(
            step_id=step_id,
            data_schema=vol.Schema({vol.Required(field, default=False): bool}),
            errors=errors,
        )

    async def async_step_calibrate_sensor(self, user_input=None):
        """Choose one hardware-validated sensor calibration assistant."""
        menu: dict[str, str] = {}
        if self._ph_available():
            menu["calibrate_ph"] = "pH"
        if self._orp_available():
            menu["calibrate_orp_prepare"] = "Redox / ORP · 470 mV"
        if self._temperature_available():
            menu["calibrate_temperature"] = "Température"
        if not menu:
            return self.async_abort(reason="maintenance_unsupported")
        return self.async_show_menu(step_id="calibrate_sensor", menu_options=menu)

    async def async_step_calibrate_ph(self, user_input=None):
        """Choose Fast or two-point pH calibration."""
        if not self._ph_available():
            return self.async_abort(reason="maintenance_unsupported")
        return self.async_show_menu(
            step_id="calibrate_ph",
            menu_options={
                "calibrate_ph_fast": "Rapide (Fast)",
                "calibrate_ph_standard_prepare": "Standard · pH 7 puis pH 4",
            },
        )

    async def async_step_calibrate_ph_fast(self, user_input=None):
        """Run Fast pH calibration with the probe left in normal circulation."""
        if not self._ph_available():
            return self.async_abort(reason="maintenance_unsupported")

        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                response = await async_calibrate_ph_fast(
                    self._config_entry.runtime_data.api,
                    float(user_input["reference_ph"]),
                )
                await self._config_entry.runtime_data.async_request_refresh()
            except (SmartNextCommunicationError, OSError, TimeoutError):
                errors["base"] = "calibration_communication_failed"
            except GuidedCalibrationError as err:
                errors["base"] = err.reason
            else:
                if response == RESPONSE_OK:
                    return self.async_abort(reason="ph_fast_ok")
                errors["base"] = self._response_error_key(response)

        current = self._config_entry.runtime_data.data.get("ph")
        default_ph = (
            float(current)
            if isinstance(current, (int, float)) and 0 <= float(current) <= 12
            else 7.0
        )
        return self.async_show_form(
            step_id="calibrate_ph_fast",
            data_schema=vol.Schema(
                {vol.Required("reference_ph", default=default_ph): _PH_SELECTOR}
            ),
            errors=errors,
            description_placeholders={"current_ph": f"{default_ph:.2f}"},
        )

    # ---------------------------------------------------------------------
    # pH Standard — persistent software safety, then exact hydraulic steps
    # ---------------------------------------------------------------------

    async def async_step_calibrate_ph_standard_prepare(self, user_input=None):
        """Force persistent 0 % electrolysis before any physical manipulation."""
        if not self._ph_available():
            return self.async_abort(reason="maintenance_unsupported")
        errors: dict[str, str] = {}
        if user_input is not None:
            if not user_input.get("confirm", False):
                errors["base"] = "confirmation_required"
            else:
                try:
                    self._ph_saved_state = await async_prepare_bypassed_calibration(
                        self._config_entry.runtime_data.api
                    )
                    await self._config_entry.runtime_data.async_request_refresh()
                except (SmartNextCommunicationError, OSError, TimeoutError):
                    errors["base"] = "calibration_communication_failed"
                except GuidedCalibrationError as err:
                    errors["base"] = err.reason
                else:
                    return await self.async_step_calibrate_ph_standard_filtration_off()
        return self.async_show_form(
            step_id="calibrate_ph_standard_prepare",
            data_schema=vol.Schema({vol.Required("confirm", default=False): bool}),
            errors=errors,
        )

    async def async_step_calibrate_ph_standard_filtration_off(self, user_input=None):
        if self._ph_saved_state is None:
            return await self.async_step_calibrate_ph_standard_prepare()
        return await self._async_confirmation_step(
            step_id="calibrate_ph_standard_filtration_off",
            field="filtration_off",
            user_input=user_input,
            next_step=self.async_step_calibrate_ph_standard_bypass_open,
        )

    async def async_step_calibrate_ph_standard_bypass_open(self, user_input=None):
        if self._ph_saved_state is None:
            return await self.async_step_calibrate_ph_standard_prepare()
        return await self._async_confirmation_step(
            step_id="calibrate_ph_standard_bypass_open",
            field="bypass_open",
            user_input=user_input,
            next_step=self.async_step_calibrate_ph_standard_inlet_closed,
        )

    async def async_step_calibrate_ph_standard_inlet_closed(self, user_input=None):
        if self._ph_saved_state is None:
            return await self.async_step_calibrate_ph_standard_prepare()
        return await self._async_confirmation_step(
            step_id="calibrate_ph_standard_inlet_closed",
            field="inlet_closed",
            user_input=user_input,
            next_step=self.async_step_calibrate_ph_standard_outlet_closed,
        )

    async def async_step_calibrate_ph_standard_outlet_closed(self, user_input=None):
        if self._ph_saved_state is None:
            return await self.async_step_calibrate_ph_standard_prepare()
        return await self._async_confirmation_step(
            step_id="calibrate_ph_standard_outlet_closed",
            field="outlet_closed",
            user_input=user_input,
            next_step=self.async_step_calibrate_ph_standard_probe_loosened,
        )

    async def async_step_calibrate_ph_standard_probe_loosened(self, user_input=None):
        if self._ph_saved_state is None:
            return await self.async_step_calibrate_ph_standard_prepare()
        return await self._async_confirmation_step(
            step_id="calibrate_ph_standard_probe_loosened",
            field="probe_loosened",
            user_input=user_input,
            next_step=self.async_step_calibrate_ph_standard_drain_pulse,
        )

    async def async_step_calibrate_ph_standard_drain_pulse(self, user_input=None):
        """Confirm the <=2 s outlet-valve pulse, then start 201 -> 203."""
        if self._ph_saved_state is None:
            return await self.async_step_calibrate_ph_standard_prepare()
        errors: dict[str, str] = {}
        if user_input is not None:
            if not user_input.get("drain_done", False):
                errors["base"] = "confirmation_required"
            else:
                try:
                    await async_begin_bypassed_calibration(
                        self._config_entry.runtime_data.api
                    )
                    await self._config_entry.runtime_data.async_request_refresh()
                except (SmartNextCommunicationError, OSError, TimeoutError):
                    errors["base"] = "calibration_communication_failed"
                except GuidedCalibrationError as err:
                    errors["base"] = err.reason
                else:
                    return await self.async_step_calibrate_ph_standard_ph7()
        return self.async_show_form(
            step_id="calibrate_ph_standard_drain_pulse",
            data_schema=vol.Schema({vol.Required("drain_done", default=False): bool}),
            errors=errors,
        )

    async def async_step_calibrate_ph_standard_ph7(self, user_input=None):
        """Guide and execute the first pH 7 calibration point."""
        if self._ph_saved_state is None:
            return await self.async_step_calibrate_ph_standard_prepare()
        errors: dict[str, str] = {}
        if user_input is not None:
            if not user_input.get("stable", False):
                errors["base"] = "confirmation_required"
            else:
                try:
                    response = await async_trigger_ph7(
                        self._config_entry.runtime_data.api
                    )
                    await self._config_entry.runtime_data.async_request_refresh()
                except (SmartNextCommunicationError, OSError, TimeoutError):
                    self._ph_last_error = "communication"
                    return await self.async_step_calibrate_ph_standard_error()
                except GuidedCalibrationError as err:
                    self._ph_last_error = err.reason
                    return await self.async_step_calibrate_ph_standard_error()
                if response == RESPONSE_FIRST_POINT_OK:
                    return await self.async_step_calibrate_ph_standard_ph4()
                self._ph_last_error = response
                return await self.async_step_calibrate_ph_standard_error()

        current = self._config_entry.runtime_data.data.get("ph")
        current_text = (
            f"{float(current):.2f}" if isinstance(current, (int, float)) else "—"
        )
        return self.async_show_form(
            step_id="calibrate_ph_standard_ph7",
            data_schema=vol.Schema({vol.Required("stable", default=False): bool}),
            errors=errors,
            description_placeholders={"current_ph": current_text},
        )

    async def async_step_calibrate_ph_standard_ph4(self, user_input=None):
        """Guide and execute the second pH 4 calibration point."""
        if self._ph_saved_state is None:
            return await self.async_step_calibrate_ph_standard_prepare()
        errors: dict[str, str] = {}
        if user_input is not None:
            if not user_input.get("stable", False):
                errors["base"] = "confirmation_required"
            else:
                try:
                    response = await async_trigger_ph4(
                        self._config_entry.runtime_data.api
                    )
                    await self._config_entry.runtime_data.async_request_refresh()
                except (SmartNextCommunicationError, OSError, TimeoutError):
                    self._ph_last_error = "communication"
                    return await self.async_step_calibrate_ph_standard_error()
                except GuidedCalibrationError as err:
                    self._ph_last_error = err.reason
                    return await self.async_step_calibrate_ph_standard_error()
                if response == RESPONSE_OK:
                    return await self.async_step_calibrate_ph_standard_restore()
                self._ph_last_error = response
                return await self.async_step_calibrate_ph_standard_error()

        current = self._config_entry.runtime_data.data.get("ph")
        current_text = (
            f"{float(current):.2f}" if isinstance(current, (int, float)) else "—"
        )
        return self.async_show_form(
            step_id="calibrate_ph_standard_ph4",
            data_schema=vol.Schema({vol.Required("stable", default=False): bool}),
            errors=errors,
            description_placeholders={"current_ph": current_text},
        )

    async def async_step_calibrate_ph_standard_error(self, user_input=None):
        """Keep 0x201 protected and let the user retry or restore hydraulics."""
        if self._ph_saved_state is None:
            return await self.async_step_calibrate_ph_standard_prepare()
        try:
            await async_rearm_calibration_mode(self._config_entry.runtime_data.api)
        except (SmartNextCommunicationError, GuidedCalibrationError, OSError, TimeoutError):
            pass
        return self.async_show_menu(
            step_id="calibrate_ph_standard_error",
            menu_options={
                "calibrate_ph_standard_retry": "Recommencer depuis le point pH 7",
                "calibrate_ph_standard_restore": "Terminer et rétablir l’installation",
            },
            description_placeholders={"error": self._response_text(self._ph_last_error)},
        )

    async def async_step_calibrate_ph_standard_retry(self, user_input=None):
        if self._ph_saved_state is None:
            return await self.async_step_calibrate_ph_standard_prepare()
        try:
            await async_restart_standard_ph_after_error(
                self._config_entry.runtime_data.api
            )
        except (SmartNextCommunicationError, OSError, TimeoutError):
            self._ph_last_error = "communication"
            return await self.async_step_calibrate_ph_standard_error()
        except GuidedCalibrationError as err:
            self._ph_last_error = err.reason
            return await self.async_step_calibrate_ph_standard_error()
        self._ph_last_error = None
        return await self.async_step_calibrate_ph_standard_ph7()

    # Reverse the hydraulic sequence before restoring logical flow + production.
    async def async_step_calibrate_ph_standard_restore(self, user_input=None):
        if self._ph_saved_state is None:
            return await self.async_step_calibrate_ph_standard_prepare()
        return await self._async_confirmation_step(
            step_id="calibrate_ph_standard_restore",
            field="probe_installed",
            user_input=user_input,
            next_step=self.async_step_calibrate_ph_standard_restore_inlet,
        )

    async def async_step_calibrate_ph_standard_restore_inlet(self, user_input=None):
        return await self._async_confirmation_step(
            step_id="calibrate_ph_standard_restore_inlet",
            field="inlet_open",
            user_input=user_input,
            next_step=self.async_step_calibrate_ph_standard_restore_outlet,
        )

    async def async_step_calibrate_ph_standard_restore_outlet(self, user_input=None):
        return await self._async_confirmation_step(
            step_id="calibrate_ph_standard_restore_outlet",
            field="outlet_open",
            user_input=user_input,
            next_step=self.async_step_calibrate_ph_standard_restore_bypass,
        )

    async def async_step_calibrate_ph_standard_restore_bypass(self, user_input=None):
        return await self._async_confirmation_step(
            step_id="calibrate_ph_standard_restore_bypass",
            field="bypass_closed",
            user_input=user_input,
            next_step=self.async_step_calibrate_ph_standard_restore_filtration,
        )

    async def async_step_calibrate_ph_standard_restore_filtration(self, user_input=None):
        if self._ph_saved_state is None:
            return await self.async_step_calibrate_ph_standard_prepare()
        errors: dict[str, str] = {}
        if user_input is not None:
            if not user_input.get("filtration_on", False):
                errors["base"] = "confirmation_required"
            else:
                try:
                    await async_restore_bypassed_calibration(
                        self._config_entry.runtime_data.api,
                        self._ph_saved_state,
                    )
                    await self._config_entry.runtime_data.async_request_refresh()
                except (SmartNextCommunicationError, OSError, TimeoutError):
                    errors["base"] = "calibration_communication_failed"
                except GuidedCalibrationError as err:
                    errors["base"] = err.reason
                else:
                    self._ph_saved_state = None
                    self._ph_last_error = None
                    return self.async_abort(reason="ph_standard_ok")
        return self.async_show_form(
            step_id="calibrate_ph_standard_restore_filtration",
            data_schema=vol.Schema({vol.Required("filtration_on", default=False): bool}),
            errors=errors,
        )

    # ---------------------------------------------------------------------
    # ORP — same exact hydraulic preparation / restoration sequence
    # ---------------------------------------------------------------------

    async def async_step_calibrate_orp_prepare(self, user_input=None):
        """Force persistent 0 % electrolysis before any physical manipulation."""
        if not self._orp_available():
            return self.async_abort(reason="maintenance_unsupported")
        errors: dict[str, str] = {}
        if user_input is not None:
            if not user_input.get("confirm", False):
                errors["base"] = "confirmation_required"
            else:
                try:
                    self._orp_saved_state = await async_prepare_bypassed_calibration(
                        self._config_entry.runtime_data.api
                    )
                    await self._config_entry.runtime_data.async_request_refresh()
                except (SmartNextCommunicationError, OSError, TimeoutError):
                    errors["base"] = "calibration_communication_failed"
                except GuidedCalibrationError as err:
                    errors["base"] = err.reason
                else:
                    return await self.async_step_calibrate_orp_filtration_off()
        return self.async_show_form(
            step_id="calibrate_orp_prepare",
            data_schema=vol.Schema({vol.Required("confirm", default=False): bool}),
            errors=errors,
        )

    async def async_step_calibrate_orp_filtration_off(self, user_input=None):
        if self._orp_saved_state is None:
            return await self.async_step_calibrate_orp_prepare()
        return await self._async_confirmation_step(
            step_id="calibrate_orp_filtration_off",
            field="filtration_off",
            user_input=user_input,
            next_step=self.async_step_calibrate_orp_bypass_open,
        )

    async def async_step_calibrate_orp_bypass_open(self, user_input=None):
        return await self._async_confirmation_step(
            step_id="calibrate_orp_bypass_open",
            field="bypass_open",
            user_input=user_input,
            next_step=self.async_step_calibrate_orp_inlet_closed,
        )

    async def async_step_calibrate_orp_inlet_closed(self, user_input=None):
        return await self._async_confirmation_step(
            step_id="calibrate_orp_inlet_closed",
            field="inlet_closed",
            user_input=user_input,
            next_step=self.async_step_calibrate_orp_outlet_closed,
        )

    async def async_step_calibrate_orp_outlet_closed(self, user_input=None):
        return await self._async_confirmation_step(
            step_id="calibrate_orp_outlet_closed",
            field="outlet_closed",
            user_input=user_input,
            next_step=self.async_step_calibrate_orp_probe_loosened,
        )

    async def async_step_calibrate_orp_probe_loosened(self, user_input=None):
        return await self._async_confirmation_step(
            step_id="calibrate_orp_probe_loosened",
            field="probe_loosened",
            user_input=user_input,
            next_step=self.async_step_calibrate_orp_drain_pulse,
        )

    async def async_step_calibrate_orp_drain_pulse(self, user_input=None):
        """Confirm the <=2 s outlet-valve pulse, then start 201 -> 203."""
        if self._orp_saved_state is None:
            return await self.async_step_calibrate_orp_prepare()
        errors: dict[str, str] = {}
        if user_input is not None:
            if not user_input.get("drain_done", False):
                errors["base"] = "confirmation_required"
            else:
                try:
                    await async_begin_bypassed_calibration(
                        self._config_entry.runtime_data.api
                    )
                    await self._config_entry.runtime_data.async_request_refresh()
                except (SmartNextCommunicationError, OSError, TimeoutError):
                    errors["base"] = "calibration_communication_failed"
                except GuidedCalibrationError as err:
                    errors["base"] = err.reason
                else:
                    return await self.async_step_calibrate_orp_470()
        return self.async_show_form(
            step_id="calibrate_orp_drain_pulse",
            data_schema=vol.Schema({vol.Required("drain_done", default=False): bool}),
            errors=errors,
        )

    async def async_step_calibrate_orp_470(self, user_input=None):
        """Guide and execute the hardware-validated 470 mV ORP calibration."""
        if self._orp_saved_state is None:
            return await self.async_step_calibrate_orp_prepare()
        errors: dict[str, str] = {}
        if user_input is not None:
            if not user_input.get("stable", False):
                errors["base"] = "confirmation_required"
            else:
                try:
                    response = await async_trigger_orp_470(
                        self._config_entry.runtime_data.api
                    )
                    await self._config_entry.runtime_data.async_request_refresh()
                except (SmartNextCommunicationError, OSError, TimeoutError):
                    self._orp_last_error = "communication"
                    return await self.async_step_calibrate_orp_error()
                except GuidedCalibrationError as err:
                    self._orp_last_error = err.reason
                    return await self.async_step_calibrate_orp_error()
                if response == RESPONSE_OK:
                    return await self.async_step_calibrate_orp_restore()
                self._orp_last_error = response
                return await self.async_step_calibrate_orp_error()

        current = self._config_entry.runtime_data.data.get("orp")
        current_text = str(int(current)) if isinstance(current, (int, float)) else "—"
        return self.async_show_form(
            step_id="calibrate_orp_470",
            data_schema=vol.Schema({vol.Required("stable", default=False): bool}),
            errors=errors,
            description_placeholders={"current_orp": current_text},
        )

    async def async_step_calibrate_orp_error(self, user_input=None):
        if self._orp_saved_state is None:
            return await self.async_step_calibrate_orp_prepare()
        try:
            await async_rearm_calibration_mode(self._config_entry.runtime_data.api)
        except (SmartNextCommunicationError, GuidedCalibrationError, OSError, TimeoutError):
            pass
        return self.async_show_menu(
            step_id="calibrate_orp_error",
            menu_options={
                "calibrate_orp_retry": "Réessayer à 470 mV",
                "calibrate_orp_restore": "Terminer et rétablir l’installation",
            },
            description_placeholders={"error": self._response_text(self._orp_last_error)},
        )

    async def async_step_calibrate_orp_retry(self, user_input=None):
        if self._orp_saved_state is None:
            return await self.async_step_calibrate_orp_prepare()
        try:
            await async_restart_orp_after_error(self._config_entry.runtime_data.api)
        except (SmartNextCommunicationError, OSError, TimeoutError):
            self._orp_last_error = "communication"
            return await self.async_step_calibrate_orp_error()
        except GuidedCalibrationError as err:
            self._orp_last_error = err.reason
            return await self.async_step_calibrate_orp_error()
        self._orp_last_error = None
        return await self.async_step_calibrate_orp_470()

    async def async_step_calibrate_orp_restore(self, user_input=None):
        if self._orp_saved_state is None:
            return await self.async_step_calibrate_orp_prepare()
        return await self._async_confirmation_step(
            step_id="calibrate_orp_restore",
            field="probe_installed",
            user_input=user_input,
            next_step=self.async_step_calibrate_orp_restore_inlet,
        )

    async def async_step_calibrate_orp_restore_inlet(self, user_input=None):
        return await self._async_confirmation_step(
            step_id="calibrate_orp_restore_inlet",
            field="inlet_open",
            user_input=user_input,
            next_step=self.async_step_calibrate_orp_restore_outlet,
        )

    async def async_step_calibrate_orp_restore_outlet(self, user_input=None):
        return await self._async_confirmation_step(
            step_id="calibrate_orp_restore_outlet",
            field="outlet_open",
            user_input=user_input,
            next_step=self.async_step_calibrate_orp_restore_bypass,
        )

    async def async_step_calibrate_orp_restore_bypass(self, user_input=None):
        return await self._async_confirmation_step(
            step_id="calibrate_orp_restore_bypass",
            field="bypass_closed",
            user_input=user_input,
            next_step=self.async_step_calibrate_orp_restore_filtration,
        )

    async def async_step_calibrate_orp_restore_filtration(self, user_input=None):
        if self._orp_saved_state is None:
            return await self.async_step_calibrate_orp_prepare()
        errors: dict[str, str] = {}
        if user_input is not None:
            if not user_input.get("filtration_on", False):
                errors["base"] = "confirmation_required"
            else:
                try:
                    await async_restore_bypassed_calibration(
                        self._config_entry.runtime_data.api,
                        self._orp_saved_state,
                    )
                    await self._config_entry.runtime_data.async_request_refresh()
                except (SmartNextCommunicationError, OSError, TimeoutError):
                    errors["base"] = "calibration_communication_failed"
                except GuidedCalibrationError as err:
                    errors["base"] = err.reason
                else:
                    self._orp_saved_state = None
                    self._orp_last_error = None
                    return self.async_abort(reason="orp_ok")
        return self.async_show_form(
            step_id="calibrate_orp_restore_filtration",
            data_schema=vol.Schema({vol.Required("filtration_on", default=False): bool}),
            errors=errors,
        )

    # ---------------------------------------------------------------------
    # Hardware-validated pH / ORP factory calibration resets
    # ---------------------------------------------------------------------

    async def async_step_restore_ph_calibration(self, user_input=None):
        if not self._ph_available():
            return self.async_abort(reason="maintenance_unsupported")
        errors: dict[str, str] = {}
        if user_input is not None:
            if not user_input.get("confirm", False):
                errors["base"] = "confirmation_required"
            else:
                try:
                    response = await async_reset_ph_calibration(
                        self._config_entry.runtime_data.api
                    )
                    await self._config_entry.runtime_data.async_request_refresh()
                except (SmartNextCommunicationError, OSError, TimeoutError):
                    errors["base"] = "calibration_communication_failed"
                except GuidedCalibrationError as err:
                    errors["base"] = err.reason
                else:
                    if response == RESPONSE_OK:
                        return self.async_abort(reason="ph_reset_ok")
                    errors["base"] = self._response_error_key(response)
        return self.async_show_form(
            step_id="restore_ph_calibration",
            data_schema=vol.Schema({vol.Required("confirm", default=False): bool}),
            errors=errors,
        )

    async def async_step_restore_orp_calibration(self, user_input=None):
        if not self._orp_available():
            return self.async_abort(reason="maintenance_unsupported")
        errors: dict[str, str] = {}
        if user_input is not None:
            if not user_input.get("confirm", False):
                errors["base"] = "confirmation_required"
            else:
                try:
                    response = await async_reset_orp_calibration(
                        self._config_entry.runtime_data.api
                    )
                    await self._config_entry.runtime_data.async_request_refresh()
                except (SmartNextCommunicationError, OSError, TimeoutError):
                    errors["base"] = "calibration_communication_failed"
                except GuidedCalibrationError as err:
                    errors["base"] = err.reason
                else:
                    if response == RESPONSE_OK:
                        return self.async_abort(reason="orp_reset_ok")
                    errors["base"] = self._response_error_key(response)
        return self.async_show_form(
            step_id="restore_orp_calibration",
            data_schema=vol.Schema({vol.Required("confirm", default=False): bool}),
            errors=errors,
        )
