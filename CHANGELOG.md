# Changelog

## [0.4.3] - 2026-07-12

### Fixed

* **Connection rejected with a non-default PIN:** HELLO always sent the factory-default PIN (`"0000"`), so a sauna with a custom PIN set on its control panel would reject every connection attempt with no error, leaving entities stuck on `unknown`.

### Added

* **Configurable PIN:** set during initial setup (discovered or manual) or later via Options, instead of hardcoded to `"0000"`.
* **PIN rejection now logged:** the controller's connect reply is parsed, so a rejected PIN now logs a clear error and is exposed as `pin_rejected` in diagnostics.

## [0.4.2] - 2026-02-06

### Fixed

* **Humidity sensor/setpoint swapped:** fields 0x13 and 0x14 were mapped in reverse — `humidity` showed the setpoint and vice versa. Confirmed by user with Tylo Sense Combi Elite. Now correctly mapped: 0x13 = current humidity, 0x14 = setpoint.

### Changed

* **Humidity sensors enabled by default:** field mapping confirmed on real hardware (Combi Elite), sensors are no longer disabled by default.

## [0.4.1] - 2026-02-01

### Added

* **Humidity sensors:** two new sensors for Combi/Steam setups — `sensor.tylo_sauna_humidity` (current humidity %) and `sensor.tylo_sauna_humidity_setpoint` (target humidity %).

## [0.4.0] - 2026-01-30

### Added

* **Programs sensor** — `sensor.tylo_sauna_programs` displays scheduled programs from the Tylo calendar tab (e.g. `09:00–12:00 Bath 88°C | 15:00–18:00 Standby FAV111`). Supports both Custom (temperature) and Favorite entries. Structured data available in attributes (`program_count`, `programs` list).
* **Debug recording mode** — toggle in integration options captures all UDP traffic (TX, RX, filtered RX) into a 2000-packet ring buffer for remote troubleshooting.
* **Download Diagnostics** — standard Home Assistant diagnostics platform. One-click download of full diagnostic JSON (config, controller state, captured packets) via Settings → Integrations → Tylo Sauna → "⋮" → Download Diagnostics.

### Changed

* **Simplified options:** removed "Allow telemetry from other IPs" toggle — smart telemetry acceptance is now always active. Removed UDP port from options (auto-discovered).
* **Telemetry host auto-recovery:** pinned telemetry host is now reset when the controller goes offline, allowing automatic re-learning on reconnect (fixes stuck connections after DHCP changes).

### Fixed

* **Memory leak on reload:** entity callbacks are now properly unregistered when entities are removed (`async_will_remove_from_hass`), preventing duplicate state updates after integration reload.

## [0.3.8] - 2026-01-27

### Fixed

* Fixed HACS installation creating nested directory structure (`tylo_sauna/tylo_sauna/`), causing integration not to load.

## [0.3.7] - 2026-01-27

### Added

* **Standby mode support**: three HVAC modes (`off` / `heat_cool` / `heat`) when standby is enabled on the physical Tylo panel.
  * `heat_cool` represents standby mode (reduced temperature heating).
  * New climate attributes: `standby_enabled`, `standby_delta_c` (temperature reduction in °C).
  * Door fault blocking now applies to both `heat` and `heat_cool` (standby) modes.

### Fixed

* Fixed duplicate `async_press` method in experimental aroma button entity.

## [0.3.6] - 2026-01-26

### Fixed

* Fixed broken installation (HACS/manual).

## [0.3.5] - 2025-12-31

### Fixed

* HACS Action: adjust `hacs.json` to match the HACS validation schema.

## [0.3.4] - 2025-12-31

### Fixed

* Hassfest: manifest key ordering and config-entry-only schema declaration (required for HACS default inclusion checks).

## [0.3.3] - 2025-12-31

### Changed

* HACS release/updates: added GitHub Actions for validation (HACS + hassfest) and automatic GitHub Releases with `tylo_sauna.zip`.
  This makes update detection in HACS more reliable (especially for custom repositories).

## [0.3.2] - 2025-12-31

### Fixed

* More correct light state synchronization (prevents false “OFF” updates while the light is still on).

## [0.3.1] - 2025-12-30

### Changed

* **Multi-device setup UX (autodiscovery)**: adding multiple controllers in a row now works more reliably.
* **Translations**: added UI translations for multiple languages (`da`, `de`, `es`, `fi`, `fr`, `it`, `nb`, `nl`, `pl`, `ru`, `sv`).

### Fixed

* Cases where adding the 2nd/3rd controller required manual setup.

## [0.3.0] - 2025-12-29

### Changed

* **Discovery-first identity (GUID-first)**: the integration now treats the controller GUID as the primary identity and tracks the runtime endpoint (`host:port`) similarly to the official app.
  This makes it significantly more resilient to sauna reboots, DHCP IP changes, and firmware variants where the effective control port changes between sessions.
* **Runtime endpoint tracking**:
  * Control port changes detected via announce now trigger a fresh HELLO/INIT handshake on the new port (some firmwares ignore commands until re-initialized).
  * Options flow now defaults the “port” field to the **current effective `control_port`** when available (less confusing in dynamic-port setups).
* **Docker/macOS robustness**: never switches the effective control host to loopback/unspecified addresses (prevents `effective_host=127.0.0.1` breaking connectivity in some Docker/network setups).

### Fixed

* Cases where the integration stayed “offline” after Home Assistant restart until the user manually corrected the port.

### Notes (migration)

* This release is designed to be **backwards compatible** for existing entries (no remove/re-add required).
* If you upgrade from older versions and see duplicated entities (for example `*_2`) or stale `restored/unavailable` entities, the cleanest path is still to remove and re-add the integration
  (see the migration note in `0.2.1`).

## [0.2.3] - 2025-12-29

### Added

* Runtime discovery listener on UDP **54377/54378** (shared across entries) to adapt to controllers that change their effective control port after reboot.
* Offline recovery probe: when offline, Home Assistant periodically sends a lightweight INIT probe to a small set of likely ports to re-establish telemetry even when broadcasts are not visible.

### Changed

* Config flow UX: if discovery found devices but the user selects **Manual**, the manual form is pre-filled with the discovered host/port (instead of defaulting to a placeholder port).

## [0.2.2] - 2025-12-29

### Fixed

* Restored runtime UI translations for the config flow/options flow by shipping `custom_components/tylo_sauna/translations/en.json`.
  Without `translations/*.json`, Home Assistant may display raw field keys (e.g. `allow_telemetry_from_other_ips`) instead of labels.

## [0.2.1] - 2025-12-29

### Added

* **Favorites (presets) support**:

  * Auto-refresh favorites from controller snapshots.
  * New entity: `select.tylo_sauna_favorite` to apply a preset as a “scene” (temperature + stop-after + light).
* **Door safety / fault telemetry (reverse-engineered)**:

  * Parses controller fault/events (e.g. door-open cancellation codes **19/20**).
  * Exposes diagnostic sensors:

    * `sensor.tylo_sauna_fault_code`
    * `sensor.tylo_sauna_fault_message`
  * `climate` blocks starting heating when the controller requires acknowledgement (prevents “silent fail”).
* **Diagnostics & connectivity UX**:

  * New diagnostic entity: `binary_sensor.tylo_sauna_online` (always available) with:

    * online/offline state
    * `last_seen` + `seconds_ago` (as attributes)
    * configured endpoint (host/port), effective telemetry source, learned control port, rx/tx counters
* **Options flow**:

  * Update host/port/name and “Allow telemetry from other IPs” without removing the integration.
* **Improved config flow UX**:

  * Two-step setup flow (pick device → confirm settings).
  * Better naming defaults (uses controller name from discovery when available).
  * Clearer user-facing label for relaxed telemetry: **“Allow telemetry from other IPs (recommended)”**.
  * English translations for config/option forms.

### Changed

* **Discovery now reads the controller’s advertised control port** from broadcast announces
  (fixes setups where the control port is not a fixed value).
* **Control port is learned automatically** from incoming Tylo packets (helps Docker and firmware variants).
* **Multi-device on same host** (sauna + steam) is supported in discovery and manual mode.
* **Device identity is stable across host changes** (uses config entry id for device identifiers).

### Fixed

* “Discovered but no data” cases where the integration sent keepalives/commands to the wrong UDP port.
* Reduced startup issues by avoiding long-running bootstrap-blocking loops; periodic jobs are handled safely.
* Improved offline UX: control entities become unavailable when the controller is offline; diagnostic entity remains available.

### Notes (migration)

* **Recommended upgrade path (best experience): remove and re-add the integration.**
  This release changes entity/device identifiers for stability (e.g., host/IP changes), which can cause duplicated entities (`*_2`) in the entity registry after an upgrade.
  The cleanest path is:
  1) Remove the **Tylo Sauna** integration (Devices & Services)
  2) Restart Home Assistant
  3) Add the integration again
* Alternative: manually remove old `restored/unavailable` entities from the entity registry if you prefer not to re-add.

## [0.1.1] - 2025-12-21

### Added

* Relaxed telemetry source filtering (optional):

  * Allows telemetry to be received from a different IP/node than the discovered control host.
  * Pins `telemetry_host` after the first valid telemetry packet.
  * Logs GUID mismatches when a GUID is present in the payload.
* Diagnostics:

  * `telemetry_host`, `rx_packets`, `tx_packets` exposed as climate extra attributes.
  * Additional debug logs around telemetry source filtering.

### Fixed

* Improved support for multi-node setups (e.g. sauna + steam) where telemetry may originate from a different node/IP.

## [0.1.0] - 2025-12-08

### Added

* Initial release of the Tylo Sauna integration for Home Assistant.
* Climate entity:

  * Heating on/off (`heat` / `off` HVAC modes)
  * Target & current temperature in °C
  * Attributes:

    * `stop_after_min` – configured *Stop after* timer (minutes)
    * `stop_remaining_min` – remaining countdown to auto-off (minutes)
* Light entity for sauna light (on/off).
* Number entity for *Stop after* timer configuration (minutes).
* Sensor entity for remaining time to auto-off (minutes).
* Local UDP protocol implementation (no cloud required).
* Basic UDP discovery in the config flow (same mechanism as the official app).
