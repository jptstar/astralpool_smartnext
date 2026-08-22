"""Regression tests for hardware-validated Smart Next pH/ORP workflows."""

from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path("custom_components/astralpool/devices/smartnext")
GUIDED = ROOT / "guided_calibration.py"
OPTIONS = ROOT / "guided_options.py"
CONFIG_FLOW = Path("custom_components/astralpool/config_flow.py")


def _source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _function_source(source: str, name: str, next_name: str | None = None) -> str:
    start = source.index(f"async def {name}")
    if next_name is None:
        return source[start:]
    end = source.index(f"async def {next_name}", start)
    return source[start:end]


def test_validated_protocol_constants_are_present() -> None:
    source = _source(GUIDED)
    assert "COIL_CALIBRATION_MODE" in source
    assert "COIL_CALIBRATION_RESPONSE_RESET" in source
    assert "COIL_PH_CALIBRATION_PH7" in source
    assert "COIL_PH_CALIBRATION_PH4" in source
    assert "COIL_PH_CALIBRATION_FAST" in source
    assert "COIL_ORP_CALIBRATION_470MV" in source
    assert "COIL_PH_CALIBRATION_RESET" in source
    assert "COIL_ORP_CALIBRATION_RESET" in source
    assert "RESPONSE_OK: Final = 1" in source
    assert "RESPONSE_E2: Final = 2" in source
    assert "RESPONSE_E3: Final = 3" in source
    assert "RESPONSE_UNAVAILABLE: Final = 4" in source
    assert "RESPONSE_INITIALIZING: Final = 5" in source
    assert "RESPONSE_FIRST_POINT_OK: Final = 16" in source


def test_ph_fast_is_201_then_hr22_x100_then_50f() -> None:
    source = _source(GUIDED)
    function = _function_source(source, "async_calibrate_ph_fast", "async_trigger_ph7")
    start = function.index("await async_start_calibration_session(api)")
    value = function.index("raw_value = round(reference_ph * 100)")
    write = function.index("await api.async_write_register(HR_CALIBRATION_VALUE, raw_value)")
    trigger = function.index("await _async_command_edge(api, COIL_PH_CALIBRATION_FAST)")
    assert value < start < write < trigger


def test_standard_ph_expects_16_then_1() -> None:
    source = _source(GUIDED)
    ph7 = _function_source(source, "async_trigger_ph7", "async_trigger_ph4")
    ph4 = _function_source(source, "async_trigger_ph4", "async_restart_standard_ph_after_error")
    assert "COIL_PH_CALIBRATION_PH7" in ph7
    assert "RESPONSE_FIRST_POINT_OK" in ph7
    assert "COIL_PH_CALIBRATION_PH4" in ph4
    assert "RESPONSE_FIRST_POINT_OK" in ph4
    assert "await async_rearm_calibration_mode(api)" in ph4


def test_orp_470_rearms_201_after_terminal_result() -> None:
    source = _source(GUIDED)
    function = _function_source(source, "async_trigger_orp_470", "async_restart_orp_after_error")
    trigger = function.index("COIL_ORP_CALIBRATION_470MV")
    wait = function.index("response = await _async_wait_for_response")
    rearm = function.rindex("await async_rearm_calibration_mode(api)")
    assert trigger < wait < rearm


def test_bypassed_calibration_has_persistent_zero_production_safety() -> None:
    source = _source(GUIDED)
    prepare = _function_source(
        source,
        "async_prepare_bypassed_calibration",
        "async_restore_bypassed_calibration",
    )
    assert "COIL_FLOW_INTERNAL_SENSOR_ENABLE, False" in prepare
    assert "COIL_FLOW_EXTERNAL_SENSOR_ENABLE, False" in prepare
    assert "COIL_ELECTROLYSIS_BOOST, False" in prepare
    assert "COIL_ELECTROLYSIS_COVER_CONTROL_ENABLE, False" in prepare
    assert "COIL_ELECTROLYSIS_EXTERNAL_CONTROL_ENABLE, False" in prepare
    assert "COIL_ELECTROLYSIS_INTERNAL_ORP_CONTROL_ENABLE, False" in prepare
    assert "HR_ELECTROLYSIS_NORMAL_SETPOINT, 0" in prepare
    assert "production == 0 and current_raw == 0 and not running" in prepare
    assert "await async_start_calibration_session(api)" in prepare


def test_mode_rearm_window_is_ten_seconds() -> None:
    source = _source(GUIDED)
    tree = ast.parse(source)
    assignments = {
        node.target.id: node.value.value
        for node in ast.walk(tree)
        if isinstance(node, ast.AnnAssign)
        and isinstance(node.target, ast.Name)
        and isinstance(node.value, ast.Constant)
    }
    assert assignments["CALIBRATION_MODE_MAX_OFF_SECONDS"] == 10.0
    rearm = _function_source(
        source,
        "async_rearm_calibration_mode",
        "_async_command_edge",
    )
    assert "calibration_mode_rearm_too_slow" in rearm


def test_restore_releases_201_before_restoring_production() -> None:
    source = _source(GUIDED)
    restore = _function_source(
        source,
        "async_restore_bypassed_calibration",
        "async_calibrate_ph_fast",
    )
    flow_internal = restore.index("COIL_FLOW_INTERNAL_SENSOR_ENABLE")
    flow_external = restore.index("COIL_FLOW_EXTERNAL_SENSOR_ENABLE")
    mode_off = restore.index("COIL_CALIBRATION_MODE, False")
    production = restore.index("HR_ELECTROLYSIS_NORMAL_SETPOINT")
    assert flow_internal < mode_off < production
    assert flow_external < mode_off < production
    assert "flow_not_restored" in restore


def test_factory_resets_force_201_then_203_then_reset_command() -> None:
    source = _source(GUIDED)
    session = _function_source(
        source,
        "async_start_calibration_session",
        "_async_wait_for_response",
    )
    assert "if force_clear:" in session
    assert "async_clear_response_in_active_mode(api, force=True)" in session

    reset = _function_source(
        source,
        "_async_reset_calibration",
        "async_reset_ph_calibration",
    )
    start = reset.index("async_start_calibration_session(api, force_clear=True)")
    trigger = reset.index("await _async_command_edge(api, coil)")
    wait = reset.index("await _async_wait_for_response(api, RESPONSE_NONE)")
    assert start < trigger < wait


def test_options_expose_pH_orp_calibration_and_reset_steps() -> None:
    source = _source(OPTIONS)
    tree = ast.parse(source)
    methods = {
        node.name for node in ast.walk(tree) if isinstance(node, ast.AsyncFunctionDef)
    }
    expected = {
        "async_step_calibrate_ph",
        "async_step_calibrate_ph_fast",
        "async_step_calibrate_ph_standard_prepare",
        "async_step_calibrate_ph_standard_bypass",
        "async_step_calibrate_ph_standard_ph7",
        "async_step_calibrate_ph_standard_ph4",
        "async_step_calibrate_ph_standard_error",
        "async_step_calibrate_ph_standard_retry",
        "async_step_calibrate_ph_standard_restore",
        "async_step_calibrate_orp_prepare",
        "async_step_calibrate_orp_bypass",
        "async_step_calibrate_orp_470",
        "async_step_calibrate_orp_error",
        "async_step_calibrate_orp_retry",
        "async_step_calibrate_orp_restore",
        "async_step_restore_ph_calibration",
        "async_step_restore_orp_calibration",
    }
    assert expected <= methods


def test_restore_menu_includes_pH_orp_and_temperature() -> None:
    source = _source(CONFIG_FLOW)
    function = source[source.index("async def async_step_restore_calibration"):]
    function = function[: function.index("async def async_step_calibrate_temperature")]
    assert 'menu["restore_ph_calibration"] = "pH"' in function
    assert 'menu["restore_orp_calibration"] = "Redox / ORP"' in function
    assert 'menu["restore_temperature_calibration"] = "Température"' in function
