import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo

from . import DOMAIN

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities
) -> None:
    data = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if not data:
        _LOGGER.error("Tylo Sauna button: controller not found for entry %s", entry.entry_id)
        return

    controller = data["controller"]
    # Важно: не добавляем кнопки всем. Экспериментальные кнопки создаём только при включённой опции.
    if not bool(getattr(controller, "experimental_aroma", False)):
        return

    async_add_entities(
        [
            TyloSteamAromaEucalyptusOnButton(controller),
            TyloSteamAromaEucalyptusOffButton(controller),
        ]
    )
    _LOGGER.info("Tylo Steam aroma buttons added (experimental)")


class _BaseTyloButton(ButtonEntity):
    def __init__(self, controller) -> None:
        self._controller = controller
        self._device_id = getattr(controller, "device_id", controller.host)

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self._device_id)},
            name=self._controller.name,
            manufacturer="Tylo",
            model="Elite",
        )


class TyloSteamAromaEucalyptusOnButton(_BaseTyloButton):
    """Экспериментальная кнопка: включить ароматизацию (Eucalyptus)."""

    def __init__(self, controller) -> None:
        super().__init__(controller)
        self._attr_name = f"{controller.name} aroma eucalyptus ON (experimental)"
        self._attr_unique_id = f"tylo_sauna_{self._device_id}_aroma_eucalyptus_on"

    async def async_press(self) -> None:
        await self._controller.async_require_control_session()
        self._controller.aroma_eucalyptus_on()


class TyloSteamAromaEucalyptusOffButton(_BaseTyloButton):
    """Экспериментальная кнопка: выключить ароматизацию (Eucalyptus)."""

    def __init__(self, controller) -> None:
        super().__init__(controller)
        self._attr_name = f"{controller.name} aroma eucalyptus OFF (experimental)"
        self._attr_unique_id = f"tylo_sauna_{self._device_id}_aroma_eucalyptus_off"

    async def async_press(self) -> None:
        await self._controller.async_require_control_session()
        self._controller.aroma_eucalyptus_off()
