import logging
from typing import Any

from homeassistant.components.sensor import (
    SensorEntity,
    SensorDeviceClass,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo

from . import DOMAIN

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities
) -> None:
    """Set up the 'time to off' sensor entity from a config entry."""
    data = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if not data:
        _LOGGER.error(
            "Tylo Sauna sensor: controller not found for entry %s", entry.entry_id
        )
        return

    controller = data["controller"]
    entity = TyloSaunaTimeToOff(controller, entry.entry_id)
    async_add_entities([entity])
    _LOGGER.info("Tylo Sauna time-to-off sensor entity added")


class TyloSaunaTimeToOff(SensorEntity):
    """Sensor for remaining time until sauna auto-off (Stop after countdown)."""

    _attr_device_class = SensorDeviceClass.DURATION
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "min"

    def __init__(self, controller, entry_id: str) -> None:
        self._controller = controller
        self._entry_id = entry_id
        self._attr_name = f"{controller.name} time to off"
        self._attr_unique_id = f"tylo_sauna_{controller.host}_time_to_off"

    @property
    def device_info(self) -> DeviceInfo:
        """Device information shared between climate, light, number and sensor entities."""
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
        Remaining time until auto-off, in minutes.

        This reflects the controller's internal countdown, not the configured value.
        """
        if self._controller.stop_rem_min is None:
            return None
        return int(self._controller.stop_rem_min)
