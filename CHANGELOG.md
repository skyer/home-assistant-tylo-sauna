# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.1] - 2025-12-21

### Added
- Relaxed telemetry source filtering (optional):
  - Allows telemetry to be received from a different IP/node than the discovered control host.
  - Pins `telemetry_host` after the first valid telemetry packet.
  - Logs GUID mismatches when a GUID is present in the payload.
- Diagnostics:
  - `telemetry_host`, `rx_packets`, `tx_packets` exposed as climate extra attributes.
  - Additional debug logs around telemetry source filtering.

### Fixed
- Improved support for multi-node setups (e.g. sauna + steam) where telemetry may originate from a different node/IP.

## [0.1.0] - 2025-12-08

### Added

- Initial release of the Tylo Sauna integration for Home Assistant.
- Climate entity:
  - Heating on/off (`heat` / `off` HVAC modes)
  - Target & current temperature in °C
  - Attributes:
    - `stop_after_min` – configured *Stop after* timer (minutes)
    - `stop_remaining_min` – remaining countdown to auto-off (minutes)
- Light entity for sauna light (on/off).
- Number entity for *Stop after* timer configuration (minutes).
- Sensor entity for remaining time to auto-off (minutes).
- Local UDP protocol implementation (no cloud required).
- Basic UDP discovery in the config flow (same mechanism as the official app).

