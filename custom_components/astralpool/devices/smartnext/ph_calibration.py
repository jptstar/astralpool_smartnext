"""Hardware-validated Smart Next pH calibration procedures."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Final

from .calibration_debug import (
    COIL_CALIBRATION_MODE,
    COIL_CALIBRATION_RESPONSE_RESET,
    COIL_PH_CALIBRATION_FAST,
    COIL_PH_CALIBRATION_PH4,
    COIL_PH_CALIBRATION_PH7,
    HR_CALIBRATION_VALUE,
    IR_CALIBRATION_RESPONSE,
)
from .const import (
    COIL_ELECTROLYSIS_BOOST,
    COIL_ELECTROLYSIS_COVER_CONTROL_ENABLE,
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

PH_RESPONSE_TIMEOUT_SECONDS: Final = 10.0
PH_RESPONSE_POLL_SECONDS: Final = 0.10
PH_OUTPUT_STOP_TIMEOUT_SECONDS: Final = 8.0
PH_OUTPUT_STOP_POLL_SECONDS: Final = 0.25
PH_MODE_VERIFY_TIMEOUT_SECONDS: Final = 3.0
PH_COMMAND_EDGE_DELAY_SECONDS: Final = 0.05
PH_GUARD_INTERVAL_SECONDS: Final = 1.0
PH_GUARD_MAX_SECONDS: Final = 15 * 60.0
PH_MODE_MAX_OFF_SECONDS: Final = 10.0

PH_RESPONSE_NONE: Final = 0
PH_RESPONSE_OK: Final = 1
PH_RESPONSE_E2: Final = 2
PH_RESPONSE_E3: Final = 3
PH_RESPONSE_UNAVAILABLE: Final = 4
PH_RESPONSE_INITIALIZING: Final = 5
PH_RESPONSE_FIRST_POINT_OK: Final = 16
PH_RESPONSE_MODE_LOST: Final = -1


class PhCalibrationError(RuntimeError):
    """Raised when the guided pH procedure cannot keep the installation safe."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(slots=True)
class PhStandardSavedState:
    """Controller settings temporarily changed by standard pH calibration."""

    internal_flow_enabled: bool
    external_flow_enabled: bool
    normal_production_setpoint: int
    boost_enabled: bool
    cover_control_enabled: bool


async def async_read_calibration_response(api: Any) -> int:
    """Read the raw calibration result register."""
    return int((await api._read_input_registers(IR_CALIBRATION_RESPONSE, 1))[0])


async def async_read_calibration_mode(api: Any) -> bool:
    """Read the real calibration-mode coil."""
    return bool((await api._read_coils(COIL_CALIBRATION_MODE, 1))[0])


async def _async_wait_for_mode(api: Any, expected: bool) -> bool:
    deadline = asyncio.get_running_loop().time() + PH_MODE_VERIFY_TIMEOUT_SECONDS
    while asyncio.get_running_loop().time() < deadline:
        if await async_read_calibration_mode(api) is expected:
            return True
        await asyncio.sleep(PH_RESPONSE_POLL_SECONDS)
    return False


async def async_rearm_calibration_mode(api: Any) -> None:
    """Re-enable calibration mode and verify it within the safety window.

    Real-hardware tests show that both a successful calibration and an error can
    make 0x201 fall back to 0, which immediately allows electrolysis to resume.
    During a bypassed standard calibration this function is therefore called
    immediately after the terminal response has been captured.
    """
    started = asyncio.get_running_loop().time()
    if not await async_read_calibration_mode(api):
        await api.async_write_coil(COIL_CALIBRATION_MODE, True)
    if not await _async_wait_for_mode(api, True):
        raise PhCalibrationError("calibration_mode_rearm_failed")
    if asyncio.get_running_loop().time() - started >= PH_MODE_MAX_OFF_SECONDS:
        raise PhCalibrationError("calibration_mode_rearm_too_slow")


async def async_start_fresh_calibration_session(api: Any) -> None:
    """Start a fresh 0x201 session and ensure IR 0x22 is clear.

    Entering 0x201 has been validated on real hardware to clear IR 0x22 by
    itself. 0x203 is retained as a fallback if a stale response remains.
    """
    await api.async_write_coil(COIL_CALIBRATION_MODE, False)
    await asyncio.sleep(PH_COMMAND_EDGE_DELAY_SECONDS)
    await api.async_write_coil(COIL_CALIBRATION_MODE, True)
    if not await _async_wait_for_mode(api, True):
        raise PhCalibrationError("calibration_mode_not_active")

    deadline = asyncio.get_running_loop().time() + 1.5
    while asyncio.get_running_loop().time() < deadline:
        if await async_read_calibration_response(api) == PH_RESPONSE_NONE:
            return
        await asyncio.sleep(PH_RESPONSE_POLL_SECONDS)

    # Hardware normally clears the response when 0x201 is entered. 0x203 is
    # only a documented fallback and is valid once calibration mode is active.
    await api.async_write_coil(COIL_CALIBRATION_RESPONSE_RESET, False)
    await asyncio.sleep(PH_COMMAND_EDGE_DELAY_SECONDS)
    await api.async_write_coil(COIL_CALIBRATION_RESPONSE_RESET, True)
    deadline = asyncio.get_running_loop().time() + 1.5
    while asyncio.get_running_loop().time() < deadline:
        if await async_read_calibration_response(api) == PH_RESPONSE_NONE:
            return
        await asyncio.sleep(PH_RESPONSE_POLL_SECONDS)
    raise PhCalibrationError("calibration_response_not_cleared")


async def _async_command_edge(api: Any, coil: int) -> None:
    """Guarantee a real OFF -> ON edge for a volatile calibration command."""
    await api.async_write_coil(coil, False)
    await asyncio.sleep(PH_COMMAND_EDGE_DELAY_SECONDS)
    await api.async_write_coil(coil, True)


async def _async_wait_for_response_change(
    api: Any,
    previous_response: int,
    *,
    rearm_after_terminal: bool,
) -> int:
    """Capture the short-lived calibration response before 0x201 is rearmed."""
    deadline = asyncio.get_running_loop().time() + PH_RESPONSE_TIMEOUT_SECONDS
    while asyncio.get_running_loop().time() < deadline:
        response = await async_read_calibration_response(api)
        if response != previous_response:
            if rearm_after_terminal:
                await async_rearm_calibration_mode(api)
            return response

        # If the controller dropped 0x201 without leaving a response, rearm it
        # immediately instead of waiting for the normal polling timeout.
        if not await async_read_calibration_mode(api):
            response = await async_read_calibration_response(api)
            await async_rearm_calibration_mode(api)
            return response if response != previous_response else PH_RESPONSE_MODE_LOST
        await asyncio.sleep(PH_RESPONSE_POLL_SECONDS)

    return PH_RESPONSE_NONE


async def async_calibrate_ph_fast(api: Any, reference_ph: float) -> int:
    """Run the hardware-validated Fast pH calibration.

    Sequence validated on real hardware:
    0x201 ON -> HR 0x22 = pH x100 -> 0x50F -> IR 0x22 = 1 on success.
    Fast calibration keeps the probe installed in its normal hydraulic circuit,
    so no bypass safety guard is required.
    """
    raw_value = round(reference_ph * 100)
    if not 0 <= raw_value <= 1200:
        raise ValueError(f"pH calibration value out of range: {reference_ph}")

    await async_start_fresh_calibration_session(api)
    await api.async_write_register(HR_CALIBRATION_VALUE, raw_value)
    await _async_command_edge(api, COIL_PH_CALIBRATION_FAST)
    response = await _async_wait_for_response_change(
        api,
        PH_RESPONSE_NONE,
        rearm_after_terminal=False,
    )

    # Fast calibration uses the normal hydraulic path. Ensure treatment is not
    # left intentionally halted if the controller did not exit 0x201 itself.
    if await async_read_calibration_mode(api):
        await api.async_write_coil(COIL_CALIBRATION_MODE, False)
    return response


async def async_prepare_standard_ph_calibration(api: Any) -> PhStandardSavedState:
    """Stop electrolysis safely before the user operates the physical bypass."""
    flow_control = (await api._read_holding_registers(HR_FLOW_CONTROL_WORD, 1))[0]
    electrolysis_control = (
        await api._read_holding_registers(HR_ELECTROLYSIS_CONTROL_WORD, 2)
    )
    control_word = electrolysis_control[0]
    saved = PhStandardSavedState(
        internal_flow_enabled=bool(flow_control & (1 << 0)),
        external_flow_enabled=bool(flow_control & (1 << 1)),
        normal_production_setpoint=int(electrolysis_control[1]),
        boost_enabled=bool(control_word & (1 << 1)),
        cover_control_enabled=bool(control_word & (1 << 2)),
    )

    try:
        # On the validated controller the two logical flow inputs must first be
        # disabled before the normal production target can be forced to 0 %.
        # Physical circulation must still be in its normal position here.
        await api.async_write_coil(COIL_FLOW_INTERNAL_SENSOR_ENABLE, False)
        await api.async_write_coil(COIL_FLOW_EXTERNAL_SENSOR_ENABLE, False)
        if saved.boost_enabled:
            await api.async_write_coil(COIL_ELECTROLYSIS_BOOST, False)
        if saved.cover_control_enabled:
            await api.async_write_coil(
                COIL_ELECTROLYSIS_COVER_CONTROL_ENABLE, False
            )
        await api.async_write_register(HR_ELECTROLYSIS_NORMAL_SETPOINT, 0)

        deadline = asyncio.get_running_loop().time() + PH_OUTPUT_STOP_TIMEOUT_SECONDS
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
            await asyncio.sleep(PH_OUTPUT_STOP_POLL_SECONDS)
        else:
            raise PhCalibrationError("electrolysis_not_stopped")

        await async_start_fresh_calibration_session(api)
        halted = bool((await api._read_discrete_inputs(DI_TREATMENT_HALTED, 1))[0])
        if not halted:
            raise PhCalibrationError("treatment_not_halted")
        return saved
    except Exception:
        # The physical bypass has not been touched yet. Restore the controller
        # settings automatically when preparation cannot be completed.
        await async_restore_standard_ph_calibration(
            api,
            saved,
            verify_flow=False,
            leave_calibration_mode=False,
        )
        raise


async def async_trigger_ph7(api: Any) -> int:
    """Trigger the first standard point; 16 means pH 7 was accepted."""
    if not await async_read_calibration_mode(api):
        await async_rearm_calibration_mode(api)
    await _async_command_edge(api, COIL_PH_CALIBRATION_PH7)
    response = await _async_wait_for_response_change(
        api,
        PH_RESPONSE_NONE,
        rearm_after_terminal=False,
    )
    if response != PH_RESPONSE_FIRST_POINT_OK:
        # Errors terminate 0x201 on the real controller. Re-arm immediately so
        # the bypassed cell remains protected while the user decides what next.
        await async_rearm_calibration_mode(api)
    return response


async def async_trigger_ph4(api: Any) -> int:
    """Trigger the second standard point and immediately re-arm 0x201 afterward."""
    if not await async_read_calibration_mode(api):
        await async_rearm_calibration_mode(api)
    await _async_command_edge(api, COIL_PH_CALIBRATION_PH4)
    return await _async_wait_for_response_change(
        api,
        PH_RESPONSE_FIRST_POINT_OK,
        rearm_after_terminal=True,
    )


async def async_restore_standard_ph_calibration(
    api: Any,
    saved: PhStandardSavedState,
    *,
    verify_flow: bool = True,
    leave_calibration_mode: bool = True,
) -> None:
    """Restore flow supervision before allowing electrolysis production again."""
    # Keep 0x201 active until the logical flow supervision has been restored and
    # the real flow alarm confirms that the hydraulic path is safe again.
    if leave_calibration_mode:
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
            raise PhCalibrationError("flow_not_restored")

    await api.async_write_coil(COIL_CALIBRATION_MODE, False)
    await api.async_write_register(
        HR_ELECTROLYSIS_NORMAL_SETPOINT, saved.normal_production_setpoint
    )
    if saved.cover_control_enabled:
        await api.async_write_coil(COIL_ELECTROLYSIS_COVER_CONTROL_ENABLE, True)
    if saved.boost_enabled:
        await api.async_write_coil(COIL_ELECTROLYSIS_BOOST, True)
