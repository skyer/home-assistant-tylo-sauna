import asyncio
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import HomeAssistant

from .const import (
    CONF_DEBUG_RECORDING,
    CONF_GUID,
    CONF_HOST,
    CONF_NAME,
    CONF_PIN,
    CONF_PORT,
    CONF_EXPERIMENTAL_AROMA,
    DOMAIN,
    DEFAULT_CONTROL_PORT,
    DEFAULT_PIN,
)

_LOGGER = logging.getLogger(__name__)

# Важно: platform `button` подключаем, но сущности создаём только при включённых экспериментальных опциях,
# чтобы это не влияло на обычных пользователей.
PLATFORMS = ["climate", "light", "number", "sensor", "select", "binary_sensor", "button"]

# Интеграция использует только config entries (config flow) и не поддерживает YAML.
# Раньше тут был `CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)`, но в разных версиях HA
# `homeassistant.helpers.config_validation`/этот хелпер мог меняться, из-за чего интеграция могла не
# загрузиться вообще и пропадала из списка “Add integration”.
# Поэтому намеренно не определяем CONFIG_SCHEMA: это безопасно и совместимо.


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Integration init (no YAML)."""
    return True


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload entry on options update."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up a Tylo Sauna config entry."""
    from .controller import SaunaController
    from .runtime_discovery import RuntimeDiscovery
    from .storage import EndpointStore

    # Options override data (so OptionsFlow changes take effect)
    cfg = {**entry.data, **entry.options}

    host = str(cfg[CONF_HOST]).strip()
    port = int(cfg.get(CONF_PORT, DEFAULT_CONTROL_PORT))
    name = cfg.get(CONF_NAME, "Tylo Sauna")

    guid = cfg.get(CONF_GUID)  # may exist for discovery-based setup
    pin = str(cfg.get(CONF_PIN) or DEFAULT_PIN)
    experimental_aroma = bool(cfg.get(CONF_EXPERIMENTAL_AROMA, False))
    debug_recording = bool(cfg.get(CONF_DEBUG_RECORDING, False))

    controller = SaunaController(
        hass=hass,
        host=host,
        port=port,
        name=name,
        guid=guid,
        pin=pin,
        experimental_aroma=experimental_aroma,
        debug_recording=debug_recording,
    )

    # Stable id for device/entities (do NOT use host here, because host can change via OptionsFlow)
    controller.device_id = entry.entry_id
    controller.configured_host = host
    controller.configured_port = port
    controller.entry_id = entry.entry_id

    domain_data = hass.data.setdefault(DOMAIN, {})
    domain_data[entry.entry_id] = {"controller": controller, "entry": entry}

    # Ensure runtime discovery listener exists (shared across entries) to adapt to changing control ports.
    if "_runtime_discovery" not in domain_data:
        # Shared persistent endpoint cache + shared runtime discovery listener.
        store = EndpointStore(hass)
        await store.async_load()
        domain_data["_endpoint_store"] = store
        domain_data["_runtime_discovery"] = RuntimeDiscovery(hass, store)
    elif "_endpoint_store" not in domain_data:
        store = EndpointStore(hass)
        await store.async_load()
        domain_data["_endpoint_store"] = store

    # If GUID is missing (e.g. legacy entry) try to restore it from endpoint cache.
    # This is only safe when the mapping is unambiguous:
    # - exact host match, or
    # - singleton cache (only one sauna known).
    try:
        store = domain_data.get("_endpoint_store")
        if not guid and store:
            guessed_guid = store.find_guid_for_host(host) or store.singleton_guid()
            if guessed_guid:
                hass.config_entries.async_update_entry(
                    entry,
                    data={**entry.data, CONF_GUID: guessed_guid},
                )
                guid = guessed_guid
                controller.guid = guessed_guid
    except Exception:  # noqa: BLE001
        pass

    runtime_discovery: RuntimeDiscovery = domain_data["_runtime_discovery"]
    if not runtime_discovery.is_running:
        await runtime_discovery.async_start()
    runtime_discovery.register(controller)

    # If we have a GUID and a cached endpoint, prefer it as the effective runtime endpoint.
    try:
        store = domain_data.get("_endpoint_store")
        if guid and store:
            rec = store.get(str(guid))
            if rec and rec.host and rec.port:
                controller.maybe_update_host(rec.host, source=str(rec.source or "cache"), guid=str(guid))
                controller.maybe_update_control_port(rec.port, source=str(rec.source or "cache"), src_ip=rec.host, guid=str(guid))
    except Exception:  # noqa: BLE001
        pass

    # Start UDP controller (HELLO/INIT) in the background
    hass.async_create_task(controller.async_start())
    _LOGGER.info("Tylo Sauna: controller scheduled for %s:%s", host, port)

    # Start keepalive:
    # - If HA already running (common), start immediately.
    # - Otherwise start once HA is started.
    async def _start_keepalive(_event=None):
        # small delay so UDP transport is more likely created
        await asyncio.sleep(0.2)
        try:
            controller.start_keepalive()
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning("Tylo Sauna: failed to start keepalive: %s", exc)

    if hass.is_running:
        hass.async_create_task(_start_keepalive())
    else:
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, _start_keepalive)

    # Reload on options update
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    # Forward the entry to platforms
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    data = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    controller = data["controller"] if data else None

    if controller:
        try:
            await controller.async_stop()
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Tylo Sauna: error stopping controller")

    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    if unload_ok and DOMAIN in hass.data:
        domain_data = hass.data[DOMAIN]
        domain_data.pop(entry.entry_id, None)
        runtime_discovery = domain_data.get("_runtime_discovery")
        if runtime_discovery and controller:
            try:
                runtime_discovery.unregister(controller)
            except Exception:  # noqa: BLE001
                pass

        # Stop listener when no entries remain (only the shared key left).
        if runtime_discovery and len([k for k in domain_data.keys() if not str(k).startswith("_")]) == 0:
            try:
                await runtime_discovery.async_stop()
            except Exception:  # noqa: BLE001
                pass
            domain_data.pop("_runtime_discovery", None)

    return unload_ok
