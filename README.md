# Tylo Sauna – Home Assistant Custom Integration

This custom integration allows you to control and monitor **Tylo Elite** electric saunas
directly from Home Assistant.

> ⚠️ This integration is **unofficial** and is based on reverse-engineering of the
> local UDP protocol used by the Tylo Elite controller. Use at your own risk.
> There is no affiliation with Tylo / TylöHelo / Helo.

---

## Features

For each configured sauna the integration creates one **device** with the following entities:

- **Climate** – `climate.tylo_sauna`
  - Target temperature (°C)
  - Current temperature (°C)
  - HVAC modes: `off` / `heat`
  - Attributes:
    - `stop_after_min` – configured *Stop after* timer (minutes)
    - `stop_remaining_min` – remaining countdown time (minutes) until auto-off

- **Light** – `light.tylo_sauna_light`
  - Simple on/off control for the sauna light

- **Number** – `number.tylo_sauna_stop_time`
  - Auto-off timer *Stop after* (minutes)
  - Integer slider (0–600 minutes by default)
  - Changing this value sends the same UDP commands as the official app

- **Sensor** – `sensor.tylo_sauna_time_to_off`
  - Remaining time until auto-off (minutes)
  - Mirrors the controller’s internal *Stop after* countdown

All communication happens locally over UDP within your network.  
No cloud access is required.

---

## Requirements

- Tylo Elite sauna controller with Wi-Fi enabled
- Sauna and Home Assistant in the same IP subnet
- UDP ports:
  - **42156** – main control/telemetry port
  - **54377 / 54378** – discovery (same as the official mobile/desktop app)

Tested with:

- Home Assistant Core 2025.x
- Tylo Elite controller in local mode (not Elite Cloud)

---

## Installation

### Manual installation

1. Copy this repository into your Home Assistant `config` directory:

   ```text
   config/
     custom_components/
       tylo_sauna/
         __init__.py
         manifest.json
         controller.py
         climate.py
         light.py
         number.py
         sensor.py
         config_flow.py
   ```

2. Restart Home Assistant.

3. Go to **Settings → Devices & Services → Add Integration**.

4. Search for **“Tylo Sauna”** and select it.

5. The integration will listen for Tylo UDP broadcasts on the local network
   (same mechanism as the official app). If a sauna is discovered, it will be
   shown in the list. You can also choose **Enter IP manually**.

6. Complete the setup wizard. A new device **“Tylo Sauna”** should appear with
   entities:

   - `climate.tylo_sauna`
   - `light.tylo_sauna_light`
   - `number.tylo_sauna_stop_time`
   - `sensor.tylo_sauna_time_to_off`

### Installation via HACS

After adding this repository as a custom repository in HACS:

1. Open **HACS → Integrations → Custom repositories**.
2. Add:
   - **URL**: `https://github.com/skyer/home-assistant-tylo-sauna`
   - **Category**: `Integration`
3. Install **Tylo Sauna** from HACS.
4. Restart Home Assistant.
5. Add the integration via **Settings → Devices & Services → Add Integration**.

---

## Usage

### Climate entity

Use the **climate** entity to:

- turn sauna heating on/off (`hvac_mode`),
- adjust the target temperature in °C,
- read current temperature, `stop_after_min`, and `stop_remaining_min`.

The sauna controller implements the actual auto-off logic;  
Home Assistant simply reflects the configured timer and its remaining time.

### Light entity

Use the **light** entity to control the sauna light as a simple on/off switch.

### Stop time (auto-off timer)

The **number** entity:

- `number.tylo_sauna_stop_time` (unit: minutes)

controls the same **Stop after** timer that you see on the original Tylo panel/app:

- changing its value sends the same UDP commands as the official app;
- the sauna will turn heating off automatically when the internal countdown
  reaches zero.

The **sensor** entity:

- `sensor.tylo_sauna_time_to_off` (unit: minutes)

exposes the **remaining time** until the sauna turns itself off, based on the
controller’s internal countdown. This is useful for automations and notifications,
for example:

- send a notification when `stop_remaining_min < 10`,
- extend the timer when someone is still using the sauna.

---

## Notes & limitations

- This integration was tested only with Tylo Elite controllers in local mode.
  Other Tylo/Tylö models may or may not be compatible.
- All protocol details are based on reverse-engineered UDP traffic from the
  official desktop/mobile app. A future firmware update may change the protocol.
- There is no dedicated error handling for connection loss or controller reboot
  beyond Home Assistant’s own retry logic.

---

## Disclaimer

This project is a personal reverse-engineering effort and is **not** endorsed,
supported, or approved by Tylo / TylöHelo / Helo or any related company.

Use at your own risk. Saunas and high-power electric devices can be dangerous.
Always follow the manufacturer’s safety instructions and your local regulations.
