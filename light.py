import logging
from typing import Any

from homeassistant.components.light import LightEntity, ColorMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo

from . import DOMAIN

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities
) -> None:
    """Set up the light entity from a config entry."""
    data = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if not data:
        _LOGGER.error(
            "Tylo Sauna light: controller not found for entry %s", entry.entry_id
        )
        return

    controller = data["controller"]
    entity = TyloSaunaLight(controller, entry.entry_id)
    async_add_entities([entity])
    _LOGGER.info("Tylo Sauna light entity added")


class TyloSaunaLight(LightEntity):
    """Light entity for the sauna light."""

    _attr_supported_color_modes = {ColorMode.ONOFF}
    _attr_color_mode = ColorMode.ONOFF

    def __init__(self, controller, entry_id: str) -> None:
        self._controller = controller
        self._entry_id = entry_id
        self._attr_name = f"{controller.name} light"
        self._attr_unique_id = f"tylo_sauna_{controller.host}_light"

    @property
    def device_info(self) -> DeviceInfo:
        """Device information shared between climate, light and number entities."""
        return DeviceInfo(
            identifiers={(DOMAIN, self._controller.host)},
            name=self._controller.name,
            manufacturer="Tylo",
            model="Elite",
        )

    async def async_added_to_hass(self) -> None:
        """Register for state updates from the controller."""
        self._controller.register_callback(self.async_write_ha_state)

    @property
    def is_on(self) -> bool | None:
        return self._controller.light

    async def async_turn_on(self, **kwargs: Any) -> None:
        self._controller.light_on()

    async def async_turn_off(self, **kwargs: Any) -> None:
        self._controller.light_off()
