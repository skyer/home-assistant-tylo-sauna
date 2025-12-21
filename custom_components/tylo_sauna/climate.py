import logging
from typing import Any

from homeassistant.components.climate import (
    ClimateEntity,
    ClimateEntityFeature,
    HVACMode,
)
from homeassistant.const import UnitOfTemperature, ATTR_TEMPERATURE
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo

from . import DOMAIN

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities
) -> None:
    """Set up the climate entity from a config entry."""
    data = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if not data:
        _LOGGER.error(
            "Tylo Sauna climate: controller not found for entry %s", entry.entry_id
        )
        return

    controller = data["controller"]
    entity = TyloSaunaClimate(controller, entry.entry_id)
    async_add_entities([entity])
    _LOGGER.info("Tylo Sauna climate entity added")


class TyloSaunaClimate(ClimateEntity):
    """Climate entity for controlling sauna heating."""

    _attr_supported_features = ClimateEntityFeature.TARGET_TEMPERATURE
    _attr_hvac_modes = [HVACMode.HEAT, HVACMode.OFF]
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_min_temp = 40.0
    _attr_max_temp = 110.0

    def __init__(self, controller, entry_id: str) -> None:
        self._controller = controller
        self._entry_id = entry_id
        self._attr_name = controller.name
        self._attr_unique_id = f"tylo_sauna_{controller.host}_climate"

    @property
    def device_info(self) -> DeviceInfo:
        """Device information shared between entities."""
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
    def hvac_mode(self) -> HVACMode | None:
        heat = self._controller.heat
        if heat is None:
            return None
        return HVACMode.HEAT if heat else HVACMode.OFF

    @property
    def current_temperature(self) -> float | None:
        return self._controller.t_cur_c

    @property
    def target_temperature(self) -> float | None:
        return self._controller.t_set_c

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """
        Extra attributes:
        - stop_after_min (configured)
        - stop_remaining_min (countdown)
        - telemetry_host (if learned in relaxed mode)
        - rx_packets / tx_packets (basic diagnostics)
        """
        attrs: dict[str, Any] = {}
        if self._controller.stop_cfg_min is not None:
            attrs["stop_after_min"] = self._controller.stop_cfg_min
        if self._controller.stop_rem_min is not None:
            attrs["stop_remaining_min"] = self._controller.stop_rem_min

        if getattr(self._controller, "telemetry_host", None):
            attrs["telemetry_host"] = self._controller.telemetry_host

        attrs["rx_packets"] = getattr(self._controller, "rx_packets", 0)
        attrs["tx_packets"] = getattr(self._controller, "tx_packets", 0)

        return attrs

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        if hvac_mode == HVACMode.HEAT:
            self._controller.heat_on()
        elif hvac_mode == HVACMode.OFF:
            self._controller.heat_off()
        else:
            _LOGGER.warning("Tylo Sauna climate: unsupported hvac_mode %s", hvac_mode)

    async def async_set_temperature(self, **kwargs: Any) -> None:
        temp = kwargs.get(ATTR_TEMPERATURE)
        if temp is None:
            return
        await self._controller.async_set_temperature(float(temp))
