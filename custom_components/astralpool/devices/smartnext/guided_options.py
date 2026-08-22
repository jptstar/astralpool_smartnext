"""Guided Home Assistant options-flow steps for Smart Next calibration."""

from __future__ import annotations

from typing import Any

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
    async_calibrate_ph_fast,
    async_prepare_bypassed_calibration,
    async_rearm_calibration_mode,
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
            return "E2 — valeur détectée trop éloignée de la valeur attendue"
        if response == RESPONSE_E3:
            return "E3 — mesure instable"
        if response == RESPONSE_UNAVAILABLE:
            return "Calibration indisponible dans l’état actuel du Smart Next"
        if response == RESPONSE_INITIALIZING:
            return "Le Smart Next indique que le canal est encore en initialisation"
        if response == RESPONSE_MODE_LOST:
            return "Le mode calibration s’est arrêté sans résultat exploitable"
        if response == RESPONSE_NONE:
            return "Aucune réponse de calibration reçue avant le délai"
        if response == "communication":
            return "Communication Modbus interrompue pendant la calibration"
        if isinstance(response, str):
            return response
        return f"Réponse inattendue du registre 0x22 : {response}"

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

    async def async_step_calibrate_ph_standard_prepare(self, user_input=None):
        """Stop production and enter 0x201 before the user operates the bypass."""
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
                    return await self.async_step_calibrate_ph_standard_bypass()
        return self.async_show_form(
            step_id="calibrate_ph_standard_prepare",
            data_schema=vol.Schema({vol.Required("confirm", default=False): bool}),
            errors=errors,
        )

    async def async_step_calibrate_ph_standard_bypass(self, user_input=None):
        """Require confirmation that the electrolyzer cell is physically bypassed."""
        if self._ph_saved_state is None:
            return await self.async_step_calibrate_ph_standard_prepare()
        errors: dict[str, str] = {}
        if user_input is not None:
            if not user_input.get("bypass_confirmed", False):
                errors["base"] = "confirmation_required"
            else:
                return await self.async_step_calibrate_ph_standard_ph7()
        return self.async_show_form(
            step_id="calibrate_ph_standard_bypass",
            data_schema=vol.Schema(
                {vol.Required("bypass_confirmed", default=False): bool}
            ),
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
        current_text = f"{float(current):.2f}" if isinstance(current, (int, float)) else "—"
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
        current_text = f"{float(current):.2f}" if isinstance(current, (int, float)) else "—"
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
        """Clear the failed two-point session while keeping 0x201 active."""
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

    async def async_step_calibrate_ph_standard_restore(self, user_input=None):
        """Restore pH probe, hydraulics and then the saved production state."""
        if self._ph_saved_state is None:
            return await self.async_step_calibrate_ph_standard_prepare()
        errors: dict[str, str] = {}
        if user_input is not None:
            if not all(
                user_input.get(key, False)
                for key in ("probe_installed", "valves_normal", "circulation_restored")
            ):
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
            step_id="calibrate_ph_standard_restore",
            data_schema=vol.Schema(
                {
                    vol.Required("probe_installed", default=False): bool,
                    vol.Required("valves_normal", default=False): bool,
                    vol.Required("circulation_restored", default=False): bool,
                }
            ),
            errors=errors,
        )

    async def async_step_calibrate_orp_prepare(self, user_input=None):
        """Stop production and enter 0x201 before ORP probe removal."""
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
                    return await self.async_step_calibrate_orp_bypass()
        return self.async_show_form(
            step_id="calibrate_orp_prepare",
            data_schema=vol.Schema({vol.Required("confirm", default=False): bool}),
            errors=errors,
        )

    async def async_step_calibrate_orp_bypass(self, user_input=None):
        """Require physical bypass confirmation before ORP probe removal."""
        if self._orp_saved_state is None:
            return await self.async_step_calibrate_orp_prepare()
        errors: dict[str, str] = {}
        if user_input is not None:
            if not user_input.get("bypass_confirmed", False):
                errors["base"] = "confirmation_required"
            else:
                return await self.async_step_calibrate_orp_470()
        return self.async_show_form(
            step_id="calibrate_orp_bypass",
            data_schema=vol.Schema(
                {vol.Required("bypass_confirmed", default=False): bool}
            ),
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
        """Keep 0x201 active after ORP failure and offer retry or safe restoration."""
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
        """Clear ORP error while keeping the bypass protected by 0x201."""
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
        """Restore ORP probe, hydraulics and finally the previous production state."""
        if self._orp_saved_state is None:
            return await self.async_step_calibrate_orp_prepare()
        errors: dict[str, str] = {}
        if user_input is not None:
            if not all(
                user_input.get(key, False)
                for key in ("probe_installed", "valves_normal", "circulation_restored")
            ):
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
            step_id="calibrate_orp_restore",
            data_schema=vol.Schema(
                {
                    vol.Required("probe_installed", default=False): bool,
                    vol.Required("valves_normal", default=False): bool,
                    vol.Required("circulation_restored", default=False): bool,
                }
            ),
            errors=errors,
        )
