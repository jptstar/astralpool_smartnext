"""Hardware-validated Smart Next pH and ORP calibration workflows."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Final

from .calibration_debug import (
    COIL_CALIBRATION_MODE,
    COIL_CALIBRATION_RESPONSE_RESET,
    COIL_ORP_CALIBRATION_470MV,
    COIL_PH_CALIBRATION_FAST,
    COIL_PH_CALIBRATION_PH4,
    COIL_PH_CALIBRATION_PH7,
    HR_CALIBRATION_VALUE,
    IR_CALIBRATION_RESPONSE,
)
from .const import (
    COIL_ELECTROLYSIS_BOOST,
    COIL_ELECTROLYSIS_COVER_CONTROL_ENABLE,
    COIL_ELECTROLYSIS_EXTERNAL_CONTROL_ENABLE,
    COIL_FLOW_EXTERNAL_SENSOR_ENABLE,
    COIL_FLOW_INTERNAL_SENSOR_ENABLE,
    DI_ELECTROLYSIS_RUNNING,
    DI_FLOW_GENERAL,
    DI_TREATMENT_HALTED,
    HR_ELECTROLYSIS_CONTROL_WORD,
    HR_ELECTROLYSIS_NORMAL_SETPOINT,
    HR_FLOW_CONTROL_WORD,
    IR_ELECTROLYSIS_CURRENT,
    IR_ELECTROLYSIS_PRODUCTION,
)

CALIBRATION_RESPONSE_TIMEOUT_SECONDS: Final = 10.0
CALIBRATION_POLL_SECONDS: Final = 0.10
CALIBRATION_MODE_VERIFY_TIMEOUT_SECONDS: Final = 3.0
CALIBRATION_COMMAND_EDGE_SECONDS: Final = 0.05
CALIBRATION_MODE_MAX_OFF_SECONDS: Final = 10.0
OUTPUT_STOP_TIMEOUT_SECONDS: Final = 8.0
OUTPUT_STOP_POLL_SECONDS: Final = 0.25

RESPONSE_NONE: Final = 0
RESPONSE_OK: Final = 1
RESPONSE_E2: Final = 2
RESPONSE_E3: Final = 3
RESPONSE_UNAVAILABLE: Final = 4
RESPONSE_INITIALIZING: Final = 5
RESPONSE_FIRST_POINT_OK: Final = 16
RESPONSE_MODE_LOST: Final = -1


class GuidedCalibrationError(RuntimeError):
    """Raised when a guided calibration cannot maintain a safe state."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(slots=True)
class CalibrationSavedState:
    """Controller settings temporarily changed while the cell may be bypassed."""

    internal_flow_enabled: bool
    external_flow_enabled: bool
    normal_production_setpoint: int
    boost_enabled: bool
    cover_control_enabled: bool
    external_control_enabled: bool


async def async_read_calibration_response(api: Any) -> int:
    """Read the shared calibration response input register 0x22."""
    return int((await api._read_input_registers(IR_CALIBRATION_RESPONSE, 1))[0])


async def async_read_calibration_mode(api: Any) -> bool:
    """Read the real state of calibration-mode coil 0x201."""
    return bool((await api._read_coils(COIL_CALIBRATION_MODE, 1))[0])


async def _async_wait_for_mode(api: Any, expected: bool) -> bool:
    deadline = (
        asyncio.get_running_loop().time() + CALIBRATION_MODE_VERIFY_TIMEOUT_SECONDS
    )
    while asyncio.get_running_loop().time() < deadline:
        if await async_read_calibration_mode(api) is expected:
            return True
        await asyncio.sleep(CALIBRATION_POLL_SECONDS)
    return False


async def async_rearm_calibration_mode(api: Any) -> None:
    """Re-enable 0x201 immediately and verify it inside the 10-second window."""
    started = asyncio.get_running_loop().time()
    if not await async_read_calibration_mode(api):
        await api.async_write_coil(COIL_CALIBRATION_MODE, True)
    if not await _async_wait_for_mode(api, True):
        raise GuidedCalibrationError("calibration_mode_rearm_failed")
    if asyncio.get_running_loop().time() - started >= CALIBRATION_MODE_MAX_OFF_SECONDS:
        raise GuidedCalibrationError("calibration_mode_rearm_too_slow")


async def _async_command_edge(api: Any, coil: int) -> None:
    """Generate an explicit OFF -> ON edge for a volatile command coil."""
    await api.async_write_coil(coil, False)
    await asyncio.sleep(CALIBRATION_COMMAND_EDGE_SECONDS)
    await api.async_write_coil(coil, True)


async def async_clear_response_in_active_mode(api: Any) -> None:
    """Clear IR 0x22 with 0x203 while 0x201 is already active."""
    await async_rearm_calibration_mode(api)
    if await async_read_calibration_response(api) == RESPONSE_NONE:
        return

    await _async_command_edge(api, COIL_CALIBRATION_RESPONSE_RESET)
    deadline = asyncio.get_running_loop().time() + 2.0
    while asyncio.get_running_loop().time() < deadline:
        if await async_read_calibration_response(api) == RESPONSE_NONE:
            # Leave the command coil in a known released state for the next use.
            await api.async_write_coil(COIL_CALIBRATION_RESPONSE_RESET, False)
            return
        await asyncio.sleep(CALIBRATION_POLL_SECONDS)
    raise GuidedCalibrationError("calibration_response_not_cleared")


async def async_start_calibration_session(api: Any) -> None:
    """Enter 0x201, verify treatment halt, then make sure IR 0x22 is clear.

    Hardware validation shows that entering 0x201 normally clears IR 0x22 by
    itself. 0x203 is used only if a stale response remains after 0x201 is active.
    """
    if not await async_read_calibration_mode(api):
        await api.async_write_coil(COIL_CALIBRATION_MODE, True)
    if not await _async_wait_for_mode(api, True):
        raise GuidedCalibrationError("calibration_mode_not_active")

    halted = bool((await api._read_discrete_inputs(DI_TREATMENT_HALTED, 1))[0])
    if not halted:
        raise GuidedCalibrationError("treatment_not_halted")

    deadline = asyncio.get_running_loop().time() + 1.5
    while asyncio.get_running_loop().time() < deadline:
        if await async_read_calibration_response(api) == RESPONSE_NONE:
            return
        await asyncio.sleep(CALIBRATION_POLL_SECONDS)

    await async_clear_response_in_active_mode(api)


async def _async_wait_for_response(api: Any, previous_response: int) -> int:
    """Capture a response, including short-lived success values."""
    deadline = asyncio.get_running_loop().time() + CALIBRATION_RESPONSE_TIMEOUT_SECONDS
    while asyncio.get_running_loop().time() < deadline:
        response = await async_read_calibration_response(api)
        if response != previous_response:
            return response

        if not await async_read_calibration_mode(api):
            # Read once more before re-arming because a terminal result and the
            # automatic 0x201 release happen nearly together on real hardware.
            response = await async_read_calibration_response(api)
            return response if response != previous_response else RESPONSE_MODE_LOST
        await asyncio.sleep(CALIBRATION_POLL_SECONDS)
    return RESPONSE_NONE


async def async_prepare_bypassed_calibration(api: Any) -> CalibrationSavedState:
    """Establish persistent 0 % production before the user touches the bypass."""
    flow_control = (await api._read_holding_registers(HR_FLOW_CONTROL_WORD, 1))[0]
    electrolysis_control = (
        await api._read_holding_registers(HR_ELECTROLYSIS_CONTROL_WORD, 2)
    )
    control_word = electrolysis_control[0]
    saved = CalibrationSavedState(
        internal_flow_enabled=bool(flow_control & (1 << 0)),
        external_flow_enabled=bool(flow_control & (1 << 1)),
        normal_production_setpoint=int(electrolysis_control[1]),
        boost_enabled=bool(control_word & (1 << 1)),
        cover_control_enabled=bool(control_word & (1 << 2)),
        external_control_enabled=bool(control_word & (1 << 4)),
    )

    try:
        # Physical circulation is still normal at this stage. The validated
        # Smart Next requires logical flow supervision to be disabled before a
        # 0 % production setpoint can be applied reliably.
        await api.async_write_coil(COIL_FLOW_INTERNAL_SENSOR_ENABLE, False)
        await api.async_write_coil(COIL_FLOW_EXTERNAL_SENSOR_ENABLE, False)
        if saved.boost_enabled:
            await api.async_write_coil(COIL_ELECTROLYSIS_BOOST, False)
        if saved.cover_control_enabled:
            await api.async_write_coil(
                COIL_ELECTROLYSIS_COVER_CONTROL_ENABLE, False
            )
        if saved.external_control_enabled:
            await api.async_write_coil(
                COIL_ELECTROLYSIS_EXTERNAL_CONTROL_ENABLE, False
            )
        await api.async_write_register(HR_ELECTROLYSIS_NORMAL_SETPOINT, 0)

        deadline = asyncio.get_running_loop().time() + OUTPUT_STOP_TIMEOUT_SECONDS
        while asyncio.get_running_loop().time() < deadline:
            production = int(
                (await api._read_input_registers(IR_ELECTROLYSIS_PRODUCTION, 1))[0]
            )
            current_raw = int(
                (await api._read_input_registers(IR_ELECTROLYSIS_CURRENT, 1))[0]
            )
            running = bool(
                (await api._read_discrete_inputs(DI_ELECTROLYSIS_RUNNING, 1))[0]
            )
            if production == 0 and current_raw == 0 and not running:
                break
            await asyncio.sleep(OUTPUT_STOP_POLL_SECONDS)
        else:
            raise GuidedCalibrationError("electrolysis_not_stopped")

        await async_start_calibration_session(api)
        return saved
    except Exception:
        # The UI has not yet authorized any physical bypass operation, so it is
        # safe to restore the original controller settings here.
        await async_restore_bypassed_calibration(
            api,
            saved,
            verify_flow=False,
            keep_mode_active=False,
        )
        raise


async def async_restore_bypassed_calibration(
    api: Any,
    saved: CalibrationSavedState,
    *,
    verify_flow: bool = True,
    keep_mode_active: bool = True,
) -> None:
    """Restore flow supervision before releasing 0x201 and production."""
    if keep_mode_active:
        await async_rearm_calibration_mode(api)

    await api.async_write_coil(
        COIL_FLOW_INTERNAL_SENSOR_ENABLE, saved.internal_flow_enabled
    )
    await api.async_write_coil(
        COIL_FLOW_EXTERNAL_SENSOR_ENABLE, saved.external_flow_enabled
    )

    if verify_flow and (saved.internal_flow_enabled or saved.external_flow_enabled):
        await asyncio.sleep(1.0)
        flow_alarm = bool((await api._read_discrete_inputs(DI_FLOW_GENERAL, 1))[0])
        if flow_alarm:
            await async_rearm_calibration_mode(api)
            raise GuidedCalibrationError("flow_not_restored")

    # Hydraulics and logical flow protection are now confirmed safe. Release
    # calibration mode while production is still held at the temporary 0 %.
    await api.async_write_coil(COIL_CALIBRATION_MODE, False)
    await api.async_write_register(
        HR_ELECTROLYSIS_NORMAL_SETPOINT, saved.normal_production_setpoint
    )
    if saved.external_control_enabled:
        await api.async_write_coil(COIL_ELECTROLYSIS_EXTERNAL_CONTROL_ENABLE, True)
    if saved.cover_control_enabled:
        await api.async_write_coil(COIL_ELECTROLYSIS_COVER_CONTROL_ENABLE, True)
    if saved.boost_enabled:
        await api.async_write_coil(COIL_ELECTROLYSIS_BOOST, True)


async def async_calibrate_ph_fast(api: Any, reference_ph: float) -> int:
    """Run validated Fast pH calibration with the probe left installed."""
    raw_value = round(reference_ph * 100)
    if not 0 <= raw_value <= 1200:
        raise ValueError(f"pH calibration value out of range: {reference_ph}")

    await async_start_calibration_session(api)
    await api.async_write_register(HR_CALIBRATION_VALUE, raw_value)
    await _async_command_edge(api, COIL_PH_CALIBRATION_FAST)
    response = await _async_wait_for_response(api, RESPONSE_NONE)

    # Fast calibration uses normal hydraulics, so an automatic 0x201 release is
    # safe. If the controller kept it active, explicitly leave calibration mode.
    if await async_read_calibration_mode(api):
        await api.async_write_coil(COIL_CALIBRATION_MODE, False)
    return response


async def async_trigger_ph7(api: Any) -> int:
    """Run the first standard pH point; 16 means pH 7 was accepted."""
    await async_rearm_calibration_mode(api)
    if await async_read_calibration_response(api) != RESPONSE_NONE:
        await async_clear_response_in_active_mode(api)
    await _async_command_edge(api, COIL_PH_CALIBRATION_PH7)
    response = await _async_wait_for_response(api, RESPONSE_NONE)
    if response != RESPONSE_FIRST_POINT_OK:
        # Any error terminates 0x201 on the validated hardware. Re-arm before
        # returning control to the UI while the cell may still be bypassed.
        await async_rearm_calibration_mode(api)
    return response


async def async_trigger_ph4(api: Any) -> int:
    """Run the second standard pH point and protect the bypass after completion."""
    await async_rearm_calibration_mode(api)
    await _async_command_edge(api, COIL_PH_CALIBRATION_PH4)
    response = await _async_wait_for_response(api, RESPONSE_FIRST_POINT_OK)
    # Success and errors both release 0x201 on real hardware. Re-arm immediately
    # because the physical bypass has not yet been restored by the user.
    await async_rearm_calibration_mode(api)
    return response


async def async_restart_standard_ph_after_error(api: Any) -> None:
    """Keep 0x201 active, clear the error and restart from the pH 7 point."""
    await async_rearm_calibration_mode(api)
    await async_clear_response_in_active_mode(api)


async def async_trigger_orp_470(api: Any) -> int:
    """Run validated ORP calibration at 470 mV and re-protect the bypass."""
    await async_rearm_calibration_mode(api)
    if await async_read_calibration_response(api) != RESPONSE_NONE:
        await async_clear_response_in_active_mode(api)
    await _async_command_edge(api, COIL_ORP_CALIBRATION_470MV)
    response = await _async_wait_for_response(api, RESPONSE_NONE)
    # The validated controller leaves 0x201 after success and after E2. Re-arm
    # for every terminal result before returning to the guided UI.
    await async_rearm_calibration_mode(api)
    return response


async def async_restart_orp_after_error(api: Any) -> None:
    """Keep 0x201 active and clear IR 0x22 before another 470 mV attempt."""
    await async_rearm_calibration_mode(api)
    await async_clear_response_in_active_mode(api)
