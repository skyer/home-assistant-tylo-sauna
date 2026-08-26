import logging
from typing import Any

from homeassistant.components.climate import (
    ClimateEntity,
    ClimateEntityFeature,
    HVACMode,
)
from homeassistant.components.persistent_notification import async_create as pn_async_create
from homeassistant.const import UnitOfTemperature, ATTR_TEMPERATURE
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo

from . import DOMAIN
from .controller import MODE_OFF, MODE_HEAT, MODE_STANDBY

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities
) -> None:
    data = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if not data:
        _LOGGER.error("Tylo Sauna climate: controller not found for entry %s", entry.entry_id)
        return

    controller = data["controller"]
    async_add_entities([TyloSaunaClimate(controller)])
    _LOGGER.info("Tylo Sauna climate entity added")


class TyloSaunaClimate(ClimateEntity):
    _attr_supported_features = ClimateEntityFeature.TARGET_TEMPERATURE
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_min_temp = 40.0
    _attr_max_temp = 110.0

    def __init__(self, controller) -> None:
        self._controller = controller
        self._attr_name = controller.name

        # IMPORTANT: unique_id must be stable across host changes
        device_id = getattr(controller, "device_id", controller.host)
        self._attr_unique_id = f"tylo_sauna_{device_id}_climate"

    @property
    def device_info(self) -> DeviceInfo:
        device_id = getattr(self._controller, "device_id", self._controller.host)
        return DeviceInfo(
            identifiers={(DOMAIN, device_id)},
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
    def hvac_modes(self) -> list[HVACMode]:
        """Return available HVAC modes based on standby availability."""
        if getattr(self._controller, "standby_enabled", False):
            return [HVACMode.OFF, HVACMode.HEAT_COOL, HVACMode.HEAT]  # OFF, Standby, Heat
        return [HVACMode.OFF, HVACMode.HEAT]  # No standby available

    @property
    def hvac_mode(self) -> HVACMode | None:
        mode = self._controller.current_mode
        if mode == MODE_HEAT:
            return HVACMode.HEAT
        elif mode == MODE_STANDBY:
            return HVACMode.HEAT_COOL  # Standby = "Heat/Cool" in HA
        elif mode == MODE_OFF:
            return HVACMode.OFF
        # Fallback to legacy heat detection if current_mode not yet received
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
        attrs: dict[str, Any] = {}

        # Publish a minute-granularity RX heartbeat so unchanged climate state still carries
        # real controller liveness without recording every UDP packet.
        last_rx = getattr(self._controller, "last_rx_dt", None)
        if last_rx is not None:
            attrs["connection_last_seen"] = last_rx.replace(second=0, microsecond=0)

        # Detailed network diagnostics remain on the diagnostic binary_sensor.
        if self._controller.stop_cfg_min is not None:
            attrs["stop_after_min"] = self._controller.stop_cfg_min
        if self._controller.stop_rem_min is not None:
            attrs["stop_remaining_min"] = self._controller.stop_rem_min

        # Door / fault state
        attrs["door_fault_pending"] = bool(getattr(self._controller, "door_fault_pending", False))

        # Standby mode info
        attrs["standby_enabled"] = bool(getattr(self._controller, "standby_enabled", False))
        if self._controller.standby_delta_c is not None:
            attrs["standby_delta_c"] = self._controller.standby_delta_c

        return attrs

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        # Check door fault before any heating mode
        if hvac_mode in (HVACMode.HEAT, HVACMode.HEAT_COOL):
            if getattr(self._controller, "door_fault_pending", False):
                fault = getattr(self._controller, "last_fault", None)
                msg = fault.message if fault and fault.message else "Door fault requires acknowledgement"
                pn_async_create(
                    self.hass,
                    msg,
                    title=f"{self._attr_name}: blocked",
                    notification_id=f"tylo_sauna_blocked_{getattr(self._controller,'device_id', self._controller.host)}",
                )
                raise HomeAssistantError("Tylo Sauna start blocked: acknowledge door fault first")

        await self._controller.async_require_control_session()

        if hvac_mode == HVACMode.HEAT:
            self._controller.heat_on()
        elif hvac_mode == HVACMode.HEAT_COOL:  # Standby mode
            self._controller.standby()
        elif hvac_mode == HVACMode.OFF:
            self._controller.heat_off()
        else:
            _LOGGER.warning("Tylo Sauna climate: unsupported hvac_mode %s", hvac_mode)

    async def async_set_temperature(self, **kwargs: Any) -> None:
        temp = kwargs.get(ATTR_TEMPERATURE)
        if temp is None:
            return
        await self._controller.async_set_temperature(float(temp))
