# AstralPool for Home Assistant

Local Modbus integration for supported AstralPool pool equipment.

This repository combines Smart Next and Pro Elyo Touch support under a single Home Assistant domain: `astralpool`.

**Stable baseline:** version 1.0.9.

> **Unofficial project** — This is an independent community integration. It is not developed, approved, endorsed, or maintained by AstralPool or Fluidra. AstralPool, Fluidra and their product names and trademarks remain the property of their respective owners.

## Supported devices

| Device | Default Modbus Unit ID | Home Assistant platforms |
| --- | ---: | --- |
| AstralPool Smart Next | 2 | Sensors, binary sensors, numbers, selects, switches, buttons |
| AstralPool Pro Elyo Touch | 9 | Climate, sensors, binary sensors, time controls |

Pro Elyo Touch exposes the selected Silent / Smart / Turbo inverter preset, the real active inverter feedback, and the documented MODEL_Serie identification as the Home Assistant device serial number when the controller provides it.

Both devices use Modbus RTU and require an external Modbus RTU-to-TCP gateway. The integration polls locally and does not require a cloud account.

## Smart Next parameters

Smart Next exposes the verified operating values, alarms and user configuration through Home Assistant. Configuration entities are enabled by default.

Current controls include:

- normal and cover electrolysis production
- Boost mode and remaining Boost time
- polarity reversal period
- Flow Cell and Flow configuration
- cover control
- Cl mV auto and Cl EXT auto
- pH setpoint, initialization time, intelligent dosing and Pump Stop
- ORP setpoint
- temperature low/high alarm limits and alarm enable switches
- conductivity/salinity low/high alarm limits and alarm enable switches
- Bio pool mode
- ECO mode when the controller exposes the corresponding HMI Modbus point

The integration also exposes the measured pH, ORP, temperature, salinity/conductivity, electrolysis current/voltage/production, hour counters, pH/ORP alarm limits and the documented alarm/status bits.

For Smart Next software 2.00, the conductivity alarm thresholds use the verified `0xC1` / `0xC2` mapping. Older v1.70 controllers use the historical `0xC2` / `0xC3` mapping. The integration detects the active layout from the controller registers before reading or writing these limits.

## Smart Next maintenance

Open **Settings → Devices & services → AstralPool → Configure → Smart Next maintenance**.

Maintenance is separated into three guided families:

- **Restart Smart Next**
- **Calibrate a sensor**
- **Restore factory calibration**

Only procedures validated on real Smart Next hardware are exposed as guided workflows. Version 1.0.9 adds guided **pH Fast**, **pH Standard two-point**, **Redox / ORP 470 mV**, plus factory calibration reset for pH and ORP. Salinity calibration remains available only through the raw `Calibration TEST` entities until its exact sequence is validated.

### Shared calibration state machine

The Smart Next uses a common calibration mechanism:

- `0x201` enters calibration mode and stops water treatment;
- `0x203`, used while `0x201` is active, clears the shared calibration response;
- input register `0x22` reports the result: `0` no response, `1` OK, `2` E2, `3` E3, `4` unavailable, `5` initializing and `16` first point of a two-point calibration accepted;
- a terminal success or error can automatically release `0x201`, allowing electrolysis to resume.

Real-hardware testing also confirmed that simply entering `0x201` normally clears `IR 0x22`; the guided pH/ORP workflows still use an explicit `0x203` after `0x201` for a deterministic fresh session.

### Safety architecture for probe-removal procedures

The pH Standard and Redox / ORP procedures require hydraulic isolation of the electrolyzer section. AstralPool therefore does **not** rely on `0x201` alone for safety.

Before the user is allowed to touch the filtration pump or bypass valves, the integration:

1. saves the current Smart Next flow and electrolysis settings;
2. disables both logical flow inputs;
3. disables known production overrides, including Boost, cover production, external chlorine control and internal ORP production control when active;
4. forces the normal electrolysis setpoint to `0 %`;
5. verifies **electrolysis production = 0**, **cell current = 0** and **electrolysis not running**.

Only after this persistent `0 %` safety has been verified does the UI start the manual hydraulic preparation. `0x201` is intentionally activated **after** that hydraulic preparation is complete.

When a calibration command succeeds or fails, the Smart Next can release `0x201`. While the electrolyzer remains isolated, AstralPool captures `IR 0x22` and re-arms `0x201` immediately. The code enforces a maximum **10-second re-arm window**. The separate `0 %` production safety remains active throughout, so production cannot resume simply because calibration mode has dropped.

Production is restored only after the probe has been reinstalled, both isolation valves are open, the bypass is closed, filtration is running again, logical flow supervision has been restored and the Smart Next confirms normal flow.

### Exact hydraulic preparation — pH Standard and ORP

The same preparation is presented one step at a time for pH Standard and Redox / ORP:

1. keep filtration running and the hydraulic circuit in its normal position while AstralPool establishes and verifies the persistent `0 %` electrolysis safety;
2. switch **OFF** the filtration pump and physically verify that it has stopped;
3. fully **OPEN** the electrolyzer bypass valve;
4. **CLOSE** the upstream/inlet valve, on the probe side of the electrolyzer;
5. **CLOSE** the downstream/outlet valve of the electrolyzer;
6. slightly unscrew the probe that will be removed, without removing it completely;
7. very slightly open the downstream/outlet valve for **no more than 2 seconds** so a small amount of air can enter and the water level in the isolated section can drop, then close the outlet valve again immediately;
8. only then does Home Assistant start the calibration session with `0x201 ON → 0x203 → IR 0x22 = 0`;
9. the probe can then be fully removed, cleaned and placed in the calibration solution.

The brief outlet-valve opening is a manual operation. The UI explicitly requires confirmation that the valve has been reclosed and warns not to leave it open for more than two seconds.

### Guided pH Fast calibration

Fast calibration keeps the pH probe installed in normal circulation. The user enters a trusted reference pH and Home Assistant performs the validated sequence:

1. `0x201 = ON`;
2. `0x203` clears `IR 0x22`;
3. write the reference value multiplied by 100 to holding register `0x22` — for example `7.20 → 720`;
4. trigger Fast calibration `0x50F`;
5. read `IR 0x22` and show the exact result.

A successful Fast calibration returns `IR 0x22 = 1` and the controller normally releases `0x201` automatically. Because the probe and normal circulation remain in place, no hydraulic bypass procedure is used for Fast calibration.

### Guided pH Standard calibration

After the exact hydraulic preparation described above, Home Assistant has already entered `0x201`, sent `0x203` and confirmed `IR 0x22 = 0`.

The calibration itself is then guided as follows:

1. fully remove and clean the pH probe;
2. immerse it in fresh pH 7 reference solution, gently agitate it and wait about 30 seconds for a stable reading;
3. trigger `0x50D`; `IR 0x22 = 16` confirms that the pH 7 point was accepted and the calibration session remains active for the second point;
4. rinse/clean the probe, immerse it in fresh pH 4 reference solution and wait again for stability;
5. trigger `0x50E`; `IR 0x22 = 1` confirms a successful two-point calibration;
6. the Smart Next normally releases `0x201` after the terminal result, so AstralPool immediately re-arms it while the electrolyzer section is still isolated.

If `IR 0x22` returns E2/E3/4/5 or another terminal failure, AstralPool re-arms `0x201` immediately and offers either a retry from the pH 7 point or a safe restoration of the installation. The persistent `0 %` production lock is never removed during this error handling.

### Guided Redox / ORP calibration

After the same hydraulic preparation, Home Assistant starts `0x201`, clears the response with `0x203`, and confirms `IR 0x22 = 0`.

The ORP calibration is then performed with a **470 mV reference solution**, ideally around **25 °C**:

1. fully remove and clean the ORP probe;
2. immerse it in fresh 470 mV solution, gently agitate it and wait about 30 seconds for a stable reading;
3. trigger `0x80F`;
4. `IR 0x22 = 1` means success;
5. `IR 0x22 = 2` is E2 when the measured value is too far from the expected 470 mV value;
6. after any terminal result, AstralPool immediately re-arms `0x201` while the electrolyzer remains isolated.

Other documented response codes are displayed to the user even though they do not need to be intentionally reproduced during hardware testing.

### Exact hydraulic restoration — pH Standard and ORP

Successful calibration and error recovery use the same controlled restoration sequence:

1. reinstall the calibrated probe fully and tighten it correctly while filtration remains OFF and both isolation valves are still closed;
2. fully **OPEN** the upstream/inlet valve;
3. fully **OPEN** the downstream/outlet valve;
4. **CLOSE** the electrolyzer bypass valve so the normal hydraulic path again passes through the electrolyzer;
5. switch filtration **ON** and verify real water circulation through the electrolyzer;
6. Home Assistant restores both logical flow inputs and checks the Smart Next flow alarm;
7. only after flow is confirmed does Home Assistant release `0x201` and restore the saved electrolysis setpoint and production-control overrides.

If normal flow is not confirmed, production remains at `0 %` and the restoration step stays blocked.

### Guided pH / ORP factory calibration reset

The factory calibration reset sequence is also validated on real hardware and does not require probe removal or hydraulic bypass.

For pH (`0x50C`) and ORP (`0x80C`), Home Assistant executes:

1. `0x201 = ON`;
2. `0x203` to clear `IR 0x22`;
3. verify `IR 0x22 = 0`;
4. trigger the relevant reset coil;
5. read `IR 0x22` — `1` confirms success;
6. the Smart Next normally releases `0x201` automatically after the terminal result.

Errors `2`, `3`, `4` and `5` are shown to the user instead of being hidden.

### Guided temperature calibration

The temperature workflow has been validated directly on real hardware.

To apply a new reference temperature, Home Assistant:

1. writes the requested temperature multiplied by 10 to holding register `0x22`;
2. triggers temperature calibration coil `0xB0F`;
3. waits **5 seconds** for the Smart Next to apply the new calibration;
4. refreshes the device data.

Example: entering `29.0 °C` writes `290` to holding register `0x22` before triggering `0xB0F`.

To restore the factory temperature calibration, Home Assistant triggers reset coil `0xB0D`, waits **2 seconds**, then refreshes the device data.

Temperature calibration intentionally does not enter generic `Calibration_Mode` because that is not part of the physically validated temperature sequence.

### Raw calibration test entities

The raw calibration diagnostics remain exposed so protocol work can continue without hiding controller behavior.

Available entities include:

- switch: calibration mode `0x201`
- switch + button: clear calibration response `0x203`
- binary sensor: treatment halted `0x202`
- number: raw calibration value holding register `0x22`
- sensor: raw calibration response input register `0x22`
- pH switches + buttons: reset `0x50C`, pH 7 point `0x50D`, pH 4 point `0x50E`, fast calibration `0x50F`
- ORP switches + buttons: reset `0x80C`, 470 mV calibration `0x80F`
- temperature switches + buttons: reset `0xB0D`, calibration `0xB0F`
- salinity switches + buttons: reset `0xC0D`, calibration `0xC0F`

All raw test names start with **Calibration TEST** and include the Modbus address. The switches read the real Smart Next coil state and can explicitly force `OFF` then `ON`. The buttons intentionally write only `1` and add no hidden sequence.

The restart procedure remains guided. Home Assistant closes the options flow, stops normal polling, arms the documented watchdog restart, waits for the controller to reboot, restores the previous watchdog timeout and reloads the integration.

The operational **pH · Pump Stop · rearm** action remains available as a normal Home Assistant button.

No undocumented global factory reset is exposed.

## Installation

### HACS

1. Add `jptstar/astralpool` as a custom repository in HACS with category **Integration**.
2. Install **AstralPool**.
3. Restart Home Assistant.
4. Open **Settings → Devices & services → Add integration → AstralPool**.
5. Choose **Smart Next** or **Pro Elyo Touch**.
6. Enter the gateway IP address, TCP port and Modbus Unit ID.

The integration validates the selected device with a real Modbus read before creating the config entry.

The device type is selected for every config entry, so one Home Assistant installation can contain several Smart Next and Pro Elyo Touch devices at the same time.

## Architecture

The Home Assistant domain is `astralpool`. Device-specific Modbus maps remain isolated under:

- `custom_components/astralpool/devices/smartnext`
- `custom_components/astralpool/devices/elyo_touch`

The common config flow and setup layer select the appropriate driver and only load the platforms supported by that device.

## Safe migration from the separate integrations

The former custom integrations use the domains `smartnext` and `elyo_touch`. Home Assistant does not automatically move config entries between integration domains, so each device must be added again through **AstralPool**.

### Recommended reversible test

Do **not** remove the existing integrations before the first test.

1. Create a Home Assistant backup.
2. Install **AstralPool**.
3. Restart Home Assistant.
4. Temporarily **disable** the existing Smart Next or Pro Elyo Touch config entry before enabling the matching AstralPool entry. This avoids two integrations polling the same Modbus RTU device at the same time.
5. Add **AstralPool** and choose the device type.
6. Verify measurements, controls, alarms, climate functions and diagnostics.
7. If the test fails, disable/remove the AstralPool entry and re-enable the former integration.

Because the old entities are still registered during a side-by-side test, Home Assistant may temporarily give the new entities IDs ending in `_2`. This is expected.

### Final migration

Once the new AstralPool entry has been validated:

1. Note the entity IDs referenced by automations, scripts and dashboards.
2. Remove the old `smartnext` / `elyo_touch` config entries and custom integration folders.
3. Restart Home Assistant.
4. Keep or rename the new AstralPool entity IDs as required by your automations.

## Communication defaults

- TCP port: `502`
- Timeout: `5 s`
- Reconnect delay: `10 s`
- Polling interval: `5 s`
- Smart Next Unit ID: `2`
- Pro Elyo Touch Unit ID: `9`

Unit ID, timeout, reconnect delay and polling interval can be adjusted from **Settings → Devices & services → AstralPool → Configure → Communication settings**.

The gateway host/IP, TCP port and all Modbus communication parameters can also be changed later with **Settings → Devices & services → AstralPool → Reconfigure**. The new connection is validated before it is saved.

## Requirements

- Home Assistant with custom integrations enabled
- `pymodbus==3.13.1` (installed automatically from the manifest)
- A correctly configured Modbus RTU-to-TCP gateway

## Validation

The GitHub workflow checks:

- Python compilation
- JSON syntax
- unit tests for both protocol implementations
- guided Smart Next maintenance procedure tests
- HACS validation
- Home Assistant hassfest

## License

MIT
