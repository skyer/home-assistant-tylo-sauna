## Wireshark capture guide (recommended)

If the integration is discovered but shows **N/A / no values**, a short packet capture usually pinpoints the issue
(network isolation, different firmware/protocol variant, or telemetry coming from a different node/IP).

### What we want to capture

Please capture UDP traffic related to Tylo:

- Discovery: UDP **54377 / 54378**
- Control + telemetry: UDP **42156**

### Wireshark – basic steps (macOS / Windows)

1. Install Wireshark (if you don’t already have it).
2. Start Wireshark.
3. Select the **network interface** that is on the same LAN as your sauna:
   - Wi‑Fi interface if your PC/Mac is on Wi‑Fi
   - Ethernet interface if you are wired
4. Apply this **display filter**:

   ```
   udp.port == 42156 || udp.port == 54377 || udp.port == 54378
   ```

   (Tip: you can also use a capture filter, but the display filter is easier.)

5. Click **Start capturing**.
6. Reproduce the issue while capturing:

   - Open the official Tylo app (iOS/macOS/Windows)
   - Wait until it discovers the controller
   - Perform a few actions:
     - Heat ON/OFF
     - Light ON/OFF
     - Set temperature
     - Set stop time (auto-off timer)

7. Capture for ~30–60 seconds, then click **Stop**.
8. Save the capture:
   - **File → Save As…**
   - Save as `.pcapng` (default)
   - Name it something like: `tylo_capture.pcapng`

9. Attach the `.pcapng` file to the GitHub issue.

### Optional: add IP filter (if you know the sauna IP)

If you know the sauna IP (example `192.168.1.29`), you can narrow the display filter:

```
ip.addr == 192.168.1.29 && (udp.port == 42156 || udp.port == 54377 || udp.port == 54378)
```

### Notes

- Please ensure the capture is done on a device that is **on the same LAN** as the sauna controller.
- If Home Assistant runs on another machine (e.g., VM/NAS), capturing on your PC/Mac while using the official app is still useful.
- If possible, mention:
  - sauna controller model (Elite / Elite WiFi / steam, etc.)
  - firmware versions shown on the panel
  - your network topology (VLAN/guest Wi‑Fi, Docker, etc.)
