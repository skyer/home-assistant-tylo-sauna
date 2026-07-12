"""Diagnostics support for Tylo Sauna."""
from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN

REDACT_KEYS = {"host", "guid", "pin"}


def _redact(data: dict, keys: set[str] | None = None) -> dict:
    """Redact sensitive keys from a dict."""
    if keys is None:
        keys = REDACT_KEYS
    return {k: ("**REDACTED**" if k in keys else v) for k, v in data.items()}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    domain_data = hass.data.get(DOMAIN, {})
    entry_data = domain_data.get(entry.entry_id, {})
    controller = entry_data.get("controller")

    result: dict[str, Any] = {
        "config_entry": {
            "data": _redact(dict(entry.data)),
            "options": _redact(dict(entry.options)),
        },
    }

    if controller is None:
        result["controller"] = "not running"
        return result

    result["controller"] = {
        "host": "**REDACTED**",
        "configured_host": "**REDACTED**",
        "configured_port": controller.configured_port,
        "control_port": controller.control_port,
        "endpoint_source": controller.endpoint_source,
        "guid": "**REDACTED**" if controller.guid else None,
        "debug_recording": controller.debug_recording,
        "experimental_aroma": controller.experimental_aroma,
        # State
        "light": controller.light,
        "heat": controller.heat,
        "current_mode": controller.current_mode,
        "t_set_c": controller.t_set_c,
        "t_cur_c": controller.t_cur_c,
        "stop_cfg_min": controller.stop_cfg_min,
        "stop_rem_min": controller.stop_rem_min,
        "standby_enabled": controller.standby_enabled,
        "standby_delta_c": controller.standby_delta_c,
        # Diagnostics
        "rx_packets": controller.rx_packets,
        "tx_packets": controller.tx_packets,
        "last_rx_dt": str(controller.last_rx_dt) if controller.last_rx_dt else None,
        "last_rx_ip": "**REDACTED**",
        "last_rx_port": controller.last_rx_port,
        "telemetry_host": "**REDACTED**" if controller.telemetry_host else None,
        "online": controller.is_online(),
        # Faults
        "pin_rejected": controller.pin_rejected,
        "door_fault_pending": controller.door_fault_pending,
        "last_fault": {
            "code": controller.last_fault.code,
            "message": controller.last_fault.message,
        } if controller.last_fault else None,
    }

    # Debug recording buffer (IPs in packets NOT redacted — user opted in by enabling debug recording)
    result["debug_buffer"] = {
        "enabled": controller.debug_recording,
        "count": len(controller._debug_buffer),
        "max_size": controller._debug_buffer.maxlen,
        "packets": list(controller._debug_buffer),
    }

    return result
