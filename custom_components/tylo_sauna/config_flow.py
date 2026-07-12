import asyncio
import logging
import re
from dataclasses import dataclass
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import HomeAssistant, callback

from .const import CONF_DEBUG_RECORDING, CONF_PIN, DEFAULT_PIN
from .storage import EndpointStore

_LOGGER = logging.getLogger(__name__)

DOMAIN = "tylo_sauna"

UDP_DISCOVERY_PORTS = (54377, 54378)
UDP_DISCOVERY_TIMEOUT = 10.0  # seconds to listen for broadcast

UUID_RE = re.compile(
    rb"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)

# Fallback port shown in UI when discovery has no data.
# Note: the controller picks a dynamic control/telemetry UDP port and it may change after reboot.
# UI field name (user-friendly) -> stored key in entry (backward compatible)
EXPERIMENTAL_AROMA_KEY = "experimental_aroma"


def _decode_varint(buf: bytes, idx: int) -> tuple[int | None, int]:
    """Return (value, new_idx)."""
    result = 0
    shift = 0
    i = idx
    while i < len(buf):
        b = buf[i]
        result |= (b & 0x7F) << shift
        i += 1
        if not (b & 0x80):
            return result, i
        shift += 7
        if shift > 63:
            return None, idx
    return None, idx


def _iter_fields(buf: bytes):
    """Yield (field_no, wire_type, value). Value is int for varint, bytes for len-delimited."""
    i = 0
    while i < len(buf):
        key, i = _decode_varint(buf, i)
        if key is None:
            return
        field_no = int(key) >> 3
        wt = int(key) & 7

        if wt == 0:  # varint
            v, i = _decode_varint(buf, i)
            if v is None:
                return
            yield field_no, wt, int(v)
        elif wt == 2:  # length-delimited
            ln, i = _decode_varint(buf, i)
            if ln is None:
                return
            ln = int(ln)
            raw = buf[i : i + ln]
            i += ln
            yield field_no, wt, raw
        elif wt == 5:  # 32-bit
            i += 4
        elif wt == 1:  # 64-bit
            i += 8
        else:
            return


def _parse_announce(data: bytes, src_port: int) -> tuple[str | None, int | None, str | None]:
    """
    Parse Tylo announce payload:
    - GUID via regex
    - port: protobuf field 2 (varint), fallback to src_port
    - name: protobuf field 5 as repeated varint bytes OR len-delimited string (best-effort)
    """
    m = UUID_RE.search(data)
    guid = m.group(0).decode("ascii") if m else None
    if not guid:
        return None, None, None

    port: int | None = None
    name_bytes_varint: list[int] = []
    name_bytes_raw: bytes | None = None

    for field_no, wt, value in _iter_fields(data):
        if field_no == 2 and wt == 0:
            port = int(value)
        elif field_no == 5:
            if wt == 0:
                b = int(value)
                if 0 <= b <= 255:
                    name_bytes_varint.append(b)
            elif wt == 2:
                name_bytes_raw = value

    name: str | None = None
    if name_bytes_raw:
        try:
            name = name_bytes_raw.decode("utf-8", errors="replace").strip()
        except Exception:  # noqa: BLE001
            name = None
    elif name_bytes_varint:
        try:
            name = bytes(name_bytes_varint).decode("utf-8", errors="replace").strip()
        except Exception:  # noqa: BLE001
            name = None

    if port is None:
        port = int(src_port)

    return guid, port, name


@dataclass
class DiscoveredSauna:
    host: str
    guid: str
    port: int
    name: str | None = None


class _DiscoveryProtocol(asyncio.DatagramProtocol):
    """One-shot UDP discovery protocol used in the config flow."""

    def __init__(self, found: dict[str, DiscoveredSauna]):
        self.found = found

    def datagram_received(self, data: bytes, addr):
        host, src_port = addr
        guid, port, name = _parse_announce(data, src_port)
        if not guid or not port:
            return
        if guid not in self.found:
            _LOGGER.debug("Tylo Sauna discovery: found %s at %s:%s (%s)", guid, host, port, name or "no-name")
            self.found[guid] = DiscoveredSauna(host=host, guid=guid, port=int(port), name=name)


class TyloSaunaConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Config flow for Tylo Sauna."""

    VERSION = 1

    def __init__(self) -> None:
        self._discovered: dict[str, DiscoveredSauna] = {}
        self._selected_guid: str | None = None
        self._manual: bool = False

    async def _async_discover(self, hass: HomeAssistant) -> list[DiscoveredSauna]:
        """Listen for Tylo broadcasts for a short period."""
        found: dict[str, DiscoveredSauna] = {}
        loop = hass.loop
        transports: list[asyncio.DatagramTransport] = []

        for port in UDP_DISCOVERY_PORTS:
            try:
                transport, _protocol = await loop.create_datagram_endpoint(
                    lambda: _DiscoveryProtocol(found),
                    local_addr=("0.0.0.0", port),
                )
                transports.append(transport)
                _LOGGER.debug("Tylo Sauna discovery(user): listening on UDP %s", port)
            except OSError as exc:
                _LOGGER.debug("Tylo Sauna discovery(user): cannot bind %s: %s", port, exc)

        if not transports:
            _LOGGER.debug("Tylo Sauna discovery(user): no UDP sockets opened; falling back to endpoint cache")
            # If runtime discovery is already running, UDP ports may be occupied.
            # In that case, fall back to the persistent endpoint cache so multi-device setup still works.
            try:
                store = hass.data.get(DOMAIN, {}).get("_endpoint_store")
                if not store:
                    store = EndpointStore(hass)
                    await store.async_load()

                devices = [
                    DiscoveredSauna(host=rec.host, guid=str(guid), port=int(rec.port))
                    for guid, rec in store.all_records().items()
                    if rec and rec.host and rec.port
                ]
            except Exception:  # noqa: BLE001
                devices = []

            existing_entries = hass.config_entries.async_entries(DOMAIN)
            known_guids = {e.data.get("guid") for e in existing_entries if e.data.get("guid")}
            return [s for s in devices if s.guid not in known_guids]

        try:
            await asyncio.sleep(UDP_DISCOVERY_TIMEOUT)
        finally:
            for t in transports:
                t.close()

        devices = list(found.values())

        # Persist discoveries into endpoint cache (so adding multiple devices works even if
        # runtime discovery occupies the UDP ports after the first entry is created).
        try:
            store = hass.data.get(DOMAIN, {}).get("_endpoint_store")
            if not store:
                store = EndpointStore(hass)
                await store.async_load()
            for s in devices:
                try:
                    await store.set(guid=str(s.guid), host=str(s.host), port=int(s.port), source="config_flow")
                except Exception:  # noqa: BLE001
                    pass
        except Exception:  # noqa: BLE001
            pass

        # Filter only by GUID (allow multiple devices on same IP: sauna + steam)
        existing_entries = hass.config_entries.async_entries(DOMAIN)
        known_guids = {e.data.get("guid") for e in existing_entries if e.data.get("guid")}
        filtered = [s for s in devices if s.guid not in known_guids]

        _LOGGER.debug(
            "Tylo Sauna discovery(user): found %d new, %d total, %d filtered out",
            len(filtered), len(devices), len(devices) - len(filtered),
        )
        return filtered

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        """
        Step 1: Choose a discovered device or choose manual mode.
        """
        errors: dict[str, str] = {}

        # If no discoveries yet, run discovery once when the wizard opens
        if not self._discovered:
            self._discovered = {s.guid: s for s in await self._async_discover(self.hass)}

        options: dict[str, str] = {}
        for guid, sauna in self._discovered.items():
            label = f"{(sauna.name or 'Tylo') } @ {sauna.host}:{sauna.port} ({guid})"
            options[guid] = label
        options["__manual__"] = "Enter IP manually"

        if user_input is not None:
            device = user_input.get("device")
            if device == "__manual__":
                self._manual = True
                self._selected_guid = None
                return await self.async_step_confirm()

            if device in self._discovered:
                self._manual = False
                self._selected_guid = device
                return await self.async_step_confirm()

            errors["base"] = "device_not_found"

        # Show selection form (Step 1)
        if options:
            schema = vol.Schema(
                {
                    vol.Required("device", default=list(options.keys())[0]): vol.In(options),
                }
            )
            return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

        # If nothing discovered at all, go straight to confirm (manual)
        self._manual = True
        return await self.async_step_confirm()

    async def async_step_confirm(self, user_input: dict[str, Any] | None = None):
        """
        Step 2:
        - For discovered: confirm name + option.
        - For manual: host/port/name + option.
        """
        errors: dict[str, str] = {}

        if not self._manual:
            sauna = self._discovered.get(self._selected_guid or "")
            if not sauna:
                return await self.async_step_user()

            default_name = sauna.name or f"Tylo Sauna {sauna.host}"
            if user_input is not None:
                name = (user_input.get("name") or "").strip() or default_name
                pin = str(user_input.get(CONF_PIN, DEFAULT_PIN)).strip() or DEFAULT_PIN

                if not pin.isdigit():
                    errors["base"] = "invalid_pin"
                else:
                    await self.async_set_unique_id(sauna.guid)
                    self._abort_if_unique_id_configured()

                    data = {
                        "host": sauna.host,
                        "port": int(sauna.port),               # IMPORTANT
                        "name": name,
                        "guid": sauna.guid,
                        CONF_PIN: pin,
                    }
                    return self.async_create_entry(title=name, data=data)

            schema = vol.Schema(
                {
                    vol.Optional("name", default=default_name): str,
                    # PIN configured on the sauna's own control panel ("0000" if never changed).
                    vol.Optional(CONF_PIN, default=DEFAULT_PIN): str,
                }
            )

            return self.async_show_form(
                step_id="confirm",
                data_schema=schema,
                errors=errors,
                description_placeholders={
                    "host": sauna.host,
                    "port": str(sauna.port),
                    "guid": sauna.guid,
                    "name": sauna.name or "",
                },
            )

        # Manual mode
        hinted = next(iter(self._discovered.values()), None) if self._discovered else None
        hinted_host = hinted.host if hinted else ""
        hinted_port = int(hinted.port) if hinted else None
        hinted_name = (
            (hinted.name or f"Tylo Sauna {hinted_host}").strip()
            if hinted and hinted_host
            else "Tylo Sauna"
        )

        # Port is required in manual mode.
        # - If discovery found something, pre-fill with the discovered port.
        # - Otherwise leave blank (no misleading defaults).
        port_field = (
            vol.Required("port", default=int(hinted_port))
            if hinted_port is not None
            else vol.Required("port")
        )
        schema = vol.Schema(
            {
                vol.Required("host", default=hinted_host): str,
                port_field: int,
                vol.Optional("name", default=hinted_name): str,
                # PIN configured on the sauna's own control panel ("0000" if never changed).
                vol.Optional(CONF_PIN, default=DEFAULT_PIN): str,
            }
        )

        if user_input is not None:
            host = (user_input.get("host") or "").strip()
            if not host:
                errors["base"] = "invalid_host"
            else:
                try:
                    port = int(user_input.get("port"))
                except Exception:  # noqa: BLE001
                    port = 0
                if not (0 < port <= 65535):
                    errors["base"] = "invalid_port"
                    return self.async_show_form(step_id="confirm", data_schema=schema, errors=errors)

                name = (user_input.get("name") or f"Tylo Sauna {host}").strip() or f"Tylo Sauna {host}"
                pin = str(user_input.get(CONF_PIN, DEFAULT_PIN)).strip() or DEFAULT_PIN
                if not pin.isdigit():
                    errors["base"] = "invalid_pin"
                    return self.async_show_form(step_id="confirm", data_schema=schema, errors=errors)

                discovered_matches = [
                    s for s in (self._discovered.values() if self._discovered else [])
                    if s.host == host
                ]
                matched_guid = discovered_matches[0].guid if len(discovered_matches) == 1 else None

                await self.async_set_unique_id(matched_guid or f"{host}:{port}")
                self._abort_if_unique_id_configured()

                data = {
                    "host": host,
                    "port": port,
                    "name": name,
                    CONF_PIN: pin,
                }
                if matched_guid:
                    data["guid"] = matched_guid
                return self.async_create_entry(title=name, data=data)
        return self.async_show_form(step_id="confirm", data_schema=schema, errors=errors)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: config_entries.ConfigEntry):
        return TyloSaunaOptionsFlowHandler(config_entry)


class TyloSaunaOptionsFlowHandler(config_entries.OptionsFlow):
    """Options flow: edit host/port/name/option without removing integration."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self._entry = config_entry

    async def async_step_init(self, user_input: dict[str, Any] | None = None):
        errors: dict[str, str] = {}
        if user_input is not None:
            host = str(user_input["host"]).strip()
            name = str(user_input.get("name", "Tylo Sauna")).strip() or "Tylo Sauna"
            pin = str(user_input.get(CONF_PIN, DEFAULT_PIN)).strip() or DEFAULT_PIN
            experimental_aroma = bool(user_input.get(EXPERIMENTAL_AROMA_KEY, False))
            debug_recording = bool(user_input.get(CONF_DEBUG_RECORDING, False))

            if not pin.isdigit():
                errors["base"] = "invalid_pin"
            else:
                # store in entry.options
                return self.async_create_entry(
                    title="",
                    data={
                        "host": host,
                        "name": name,
                        CONF_PIN: pin,
                        EXPERIMENTAL_AROMA_KEY: experimental_aroma,
                        CONF_DEBUG_RECORDING: debug_recording,
                    },
                )

        current = {**self._entry.data, **self._entry.options}
        schema = vol.Schema(
            {
                vol.Required("host", default=current.get("host", "")): str,
                vol.Optional("name", default=current.get("name", "Tylo Sauna")): str,
                # PIN configured on the sauna's own control panel ("0000" if never changed).
                vol.Optional(CONF_PIN, default=str(current.get(CONF_PIN, DEFAULT_PIN))): str,
                # Экспериментальная опция для steam/aroma устройств (по умолчанию выключена).
                vol.Optional(
                    EXPERIMENTAL_AROMA_KEY,
                    default=bool(current.get(EXPERIMENTAL_AROMA_KEY, False)),
                ): bool,
                vol.Optional(
                    CONF_DEBUG_RECORDING,
                    default=bool(current.get(CONF_DEBUG_RECORDING, False)),
                ): bool,
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema, errors=errors)
