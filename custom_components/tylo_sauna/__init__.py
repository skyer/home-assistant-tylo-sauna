import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import HomeAssistant

from .controller import SaunaController

_LOGGER = logging.getLogger(__name__)

DOMAIN = "tylo_sauna"
PLATFORMS = ["climate", "light", "number", "sensor"]


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """
    Component initialization.
    This integration does not use YAML config; all configuration goes through the config flow.
    """
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up a Tylo Sauna config entry."""
    host = entry.data["host"]
    port = entry.data.get("port", 42156)
    name = entry.data.get("name", "Tylo Sauna")

    guid = entry.data.get("guid")
    relaxed = entry.data.get("relaxed_telemetry", True)

    controller = SaunaController(
        hass=hass,
        host=host,
        port=port,
        name=name,
        guid=guid,
        relaxed_telemetry=relaxed,
    )
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {"controller": controller}

    # Start UDP controller (HELLO/INIT) in the background
    hass.async_create_task(controller.async_start())
    _LOGGER.info("Tylo Sauna: controller scheduled for %s:%s", host, port)

    # Start keepalive loop after Home Assistant is fully started
    async def _start_keepalive(event):
        await controller.async_start_keepalive()

    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, _start_keepalive)

    # Forward the entry to platforms
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a Tylo Sauna config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok and DOMAIN in hass.data:
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unload_ok
