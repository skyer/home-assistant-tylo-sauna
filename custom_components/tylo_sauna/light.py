import logging
from typing import Any

from homeassistant.components.light import LightEntity, ColorMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo

from . import DOMAIN

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities) -> None:
    data = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if not data:
        _LOGGER.error("Tylo Sauna light: controller not found for entry %s", entry.entry_id)
        return

    controller = data["controller"]
    async_add_entities([TyloSaunaLight(controller)])
    _LOGGER.info("Tylo Sauna light entity added")


class TyloSaunaLight(LightEntity):
    _attr_supported_color_modes = {ColorMode.ONOFF}
    _attr_color_mode = ColorMode.ONOFF

    def __init__(self, controller) -> None:
        self._controller = controller
        self._device_id = getattr(controller, "device_id", controller.host)
        self._attr_name = f"{controller.name} light"
        self._attr_unique_id = f"tylo_sauna_{self._device_id}_light"

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self._device_id)},
            name=self._controller.name,
            manufacturer="Tylo",
            model="Elite",
        )

    @property
    def available(self) -> bool:
        return bool(self._controller.is_online())

    async def async_added_to_hass(self) -> None:
        self._controller.register_callback(self.async_write_ha_state)

    async def async_will_remove_from_hass(self) -> None:
        self._controller.unregister_callback(self.async_write_ha_state)

    @property
    def is_on(self) -> bool | None:
        return self._controller.light

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._controller.async_require_control_session()
        self._controller.light_on()

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._controller.async_require_control_session()
        self._controller.light_off()
