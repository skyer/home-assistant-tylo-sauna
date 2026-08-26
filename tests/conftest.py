from __future__ import annotations

import sys
import types
from datetime import UTC, datetime
from pathlib import Path


ROOT = Path(__file__).parents[1]
COMPONENT = ROOT / "custom_components" / "tylo_sauna"


def _module(name: str, **attrs):
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


class _Entity:
    pass


class _Feature:
    TARGET_TEMPERATURE = 1


class _HVACMode:
    OFF = "off"
    HEAT = "heat"
    HEAT_COOL = "heat_cool"


class _TemperatureUnit:
    CELSIUS = "°C"


class _DeviceInfo(dict):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)


homeassistant = _module("homeassistant")
_module("homeassistant.util")
_module("homeassistant.util.dt", utcnow=lambda: datetime.now(UTC))
helpers = _module("homeassistant.helpers")
def _track_time_interval(hass, callback, _interval):
    hass.tracked_intervals.append(callback)
    active = True

    def unsubscribe() -> None:
        nonlocal active
        if active:
            hass.tracked_intervals.remove(callback)
            active = False

    return unsubscribe


_module("homeassistant.helpers.event", async_track_time_interval=_track_time_interval)
_module("homeassistant.helpers.device_registry", DeviceInfo=_DeviceInfo)
components = _module("homeassistant.components")
_module(
    "homeassistant.components.climate",
    ClimateEntity=_Entity,
    ClimateEntityFeature=_Feature,
    HVACMode=_HVACMode,
)
_module(
    "homeassistant.components.persistent_notification",
    async_create=lambda *_args, **_kwargs: None,
)
_module("homeassistant.const", UnitOfTemperature=_TemperatureUnit, ATTR_TEMPERATURE="temperature")
_module("homeassistant.config_entries", ConfigEntry=object)
_module("homeassistant.core", HomeAssistant=object)
_module("homeassistant.exceptions", HomeAssistantError=RuntimeError)

setattr(homeassistant, "util", sys.modules["homeassistant.util"])
setattr(sys.modules["homeassistant.util"], "dt", sys.modules["homeassistant.util.dt"])
setattr(homeassistant, "helpers", helpers)
setattr(homeassistant, "components", components)

custom_components = _module("custom_components")
custom_components.__path__ = [str(ROOT / "custom_components")]
package = _module("custom_components.tylo_sauna", DOMAIN="tylo_sauna")
package.__path__ = [str(COMPONENT)]
