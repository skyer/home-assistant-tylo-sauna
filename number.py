import logging
from typing import Any

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo

from . import DOMAIN

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities
) -> None:
    """Set up the 'stop time' number entity from a config entry."""
    data = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if not data:
        _LOGGER.error(
            "Tylo Sauna number: controller not found for entry %s", entry.entry_id
        )
        return

    controller = data["controller"]
    entity = TyloSaunaStopTime(controller, entry.entry_id)
    async_add_entities([entity])
    _LOGGER.info("Tylo Sauna stop time number entity added")


class TyloSaunaStopTime(NumberEntity):
    """Number entity for the Stop after auto-off timer (minutes)."""

    _attr_native_min_value = 0        # can be set to 1 if you want to forbid 0
    _attr_native_max_value = 600      # 10 hours
    _attr_native_step = 1
    _attr_mode = NumberMode.SLIDER
    _attr_native_unit_of_measurement = "min"

    def __init__(self, controller, entry_id: str) -> None:
        self._controller = controller
        self._entry_id = entry_id
        self._attr_name = f"{controller.name} stop time"
        self._attr_unique_id = f"tylo_sauna_{controller.host}_stop_time"

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
    def native_value(self) -> int | None:
        """
        Expose the configured Stop after (minutes),
        not the remaining time.
        """
        if self._controller.stop_cfg_min is None:
            return None
        return int(self._controller.stop_cfg_min)

    async def async_set_native_value(self, value: float) -> None:
        """Update the Stop after timer (minutes)."""
        mins = int(round(value))
        await self._controller.async_set_stop_after(mins)
