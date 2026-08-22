"""Translation coverage checks for the combined AstralPool integration."""

import json
from pathlib import Path


ROOT = Path("custom_components/astralpool")


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_french_catalog_covers_every_source_entity() -> None:
    """Every source entity translation must have a French counterpart."""
    source = _load(ROOT / "strings.json")["entity"]
    french = _load(ROOT / "translations/fr.json")["entity"]
    assert french.keys() == source.keys()
    for platform, entities in source.items():
        assert french[platform].keys() == entities.keys()
        for key in entities:
            assert french[platform][key]["name"]


def test_select_states_match_between_source_and_french() -> None:
    """Stable select option keys must remain identical across languages."""
    source = _load(ROOT / "strings.json")["entity"]["select"]
    french = _load(ROOT / "translations/fr.json")["entity"]["select"]
    for key in ("ph_initialization_time", "polarity_reversal_period"):
        assert source[key]["state"].keys() == french[key]["state"].keys()


def test_maintenance_actions_are_fully_translated() -> None:
    """Guided maintenance procedure labels must be localized in French."""
    source = _load(ROOT / "strings.json")["selector"]["maintenance_action"]["options"]
    french = _load(ROOT / "translations/fr.json")["selector"]["maintenance_action"]["options"]
    assert french.keys() == source.keys()
    assert french["reset_ph_calibration"] == "pH · réinitialiser la calibration"
    assert french["reset_orp_calibration"] == "ORP · réinitialiser la calibration"
    assert french["restart_device"] == "Système · redémarrer le Smart Next"


def test_guided_calibration_options_are_fully_translated() -> None:
    """Every guided pH/ORP step, error and success must exist in French."""
    source = _load(ROOT / "strings.json")["options"]
    french = _load(ROOT / "translations/fr.json")["options"]
    assert french["abort"].keys() == source["abort"].keys()
    assert french["error"].keys() == source["error"].keys()
    assert french["step"].keys() == source["step"].keys()

    guided_steps = {
        "calibrate_sensor",
        "calibrate_ph",
        "calibrate_ph_fast",
        "calibrate_ph_standard_prepare",
        "calibrate_ph_standard_filtration_off",
        "calibrate_ph_standard_bypass_open",
        "calibrate_ph_standard_inlet_closed",
        "calibrate_ph_standard_outlet_closed",
        "calibrate_ph_standard_probe_loosened",
        "calibrate_ph_standard_drain_pulse",
        "calibrate_ph_standard_ph7",
        "calibrate_ph_standard_ph4",
        "calibrate_ph_standard_error",
        "calibrate_ph_standard_restore",
        "calibrate_ph_standard_restore_inlet",
        "calibrate_ph_standard_restore_outlet",
        "calibrate_ph_standard_restore_bypass",
        "calibrate_ph_standard_restore_filtration",
        "calibrate_orp_prepare",
        "calibrate_orp_filtration_off",
        "calibrate_orp_bypass_open",
        "calibrate_orp_inlet_closed",
        "calibrate_orp_outlet_closed",
        "calibrate_orp_probe_loosened",
        "calibrate_orp_drain_pulse",
        "calibrate_orp_470",
        "calibrate_orp_error",
        "calibrate_orp_restore",
        "calibrate_orp_restore_inlet",
        "calibrate_orp_restore_outlet",
        "calibrate_orp_restore_bypass",
        "calibrate_orp_restore_filtration",
        "restore_calibration",
        "restore_ph_calibration",
        "restore_orp_calibration",
    }
    assert guided_steps <= source["step"].keys()
    for step in guided_steps:
        assert french["step"][step]["title"]
