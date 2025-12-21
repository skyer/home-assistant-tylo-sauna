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
    - `telemetry_host` – telemetry source IP (only set if telemetry is received from a different node/IP)
    - `rx_packets`, `tx_packets` – basic UDP counters (diagnostics)

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

## Troubleshooting

If the integration is discovered but shows **N/A / no values**, this usually means Home Assistant is not receiving
telemetry packets back from the controller, or telemetry is coming from a different IP/node than the one discovered.

### Enable debug logging

Add this to your `configuration.yaml` (temporarily):

```yaml
logger:
  default: info
  logs:
    custom_components.tylo_sauna: debug
```

Restart Home Assistant, reproduce the problem (open the integration, try heat/light), then attach logs from
**Settings → System → Logs**.

### Packet capture (recommended)

A short packet capture helps identify whether your controller uses a different firmware/protocol variant or whether
your network blocks UDP replies.

#### tcpdump on the Home Assistant host

Run this on the machine where Home Assistant actually runs (HA OS SSH add-on, VM host, Docker host, etc.):

```bash
sudo tcpdump -i <iface> -nn -s0 -w tylo_capture.pcap \
  'udp and (port 42156 or port 54377 or port 54378)'
```

- Replace `<iface>` with your active network interface (e.g. `eth0`, `ens18`, `wlan0`).
- Keep capture running for 30–60 seconds while you:
  - open the official Tylo app,
  - toggle heat on/off,
  - toggle light on/off,
  - set temperature,
  - set stop time (auto-off timer).

Stop with `Ctrl+C` and attach `tylo_capture.pcap` to the GitHub issue.

#### Wireshark on a computer in the same LAN

Display filter:

```
udp.port == 42156 || udp.port == 54377 || udp.port == 54378
```

Start capture, reproduce actions in the official app, then export `.pcapng` and attach it.

### Wireshark capture guide (recommended)

If you need a more detailed Wireshark guide, see: `Wireshark_capture_guide.md` in this repository.

### Network checklist

- Home Assistant and the sauna controller must be in the **same IP subnet** for local discovery and UDP control.
- Avoid guest Wi-Fi / client isolation / VLAN separation unless you explicitly route UDP traffic.
- If you run HA in Docker, ensure networking allows incoming UDP replies (host networking is the simplest).

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
