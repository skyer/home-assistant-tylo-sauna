import asyncio
import logging
import re
from dataclasses import dataclass
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import HomeAssistant

from . import DOMAIN

_LOGGER = logging.getLogger(__name__)

UDP_DISCOVERY_PORTS = (54377, 54378)
UDP_DISCOVERY_TIMEOUT = 10.0  # seconds to listen for broadcast
UUID_RE = re.compile(
    rb"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)


@dataclass
class DiscoveredSauna:
    host: str
    guid: str


class _DiscoveryProtocol(asyncio.DatagramProtocol):
    """One-shot UDP discovery protocol used in the config flow."""

    def __init__(self, found: dict[str, DiscoveredSauna]):
        self.found = found

    def datagram_received(self, data: bytes, addr):
        host, _port = addr
        match = UUID_RE.search(data)
        if not match:
            return
        guid = match.group(0).decode("ascii")
        if guid not in self.found:
            _LOGGER.debug("Tylo Sauna discovery: found %s at %s", guid, host)
            self.found[guid] = DiscoveredSauna(host=host, guid=guid)


class TyloSaunaConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Config flow for Tylo Sauna."""

    VERSION = 1

    def __init__(self) -> None:
        self._discovered: dict[str, DiscoveredSauna] = {}

    async def _async_discover(self, hass: HomeAssistant) -> list[DiscoveredSauna]:
        """
        Listen for Tylo broadcasts on the local network for a short period.
        This is only used when the user opens the Add Integration wizard.
        """
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
                _LOGGER.debug(
                    "Tylo Sauna discovery(user): cannot bind %s: %s", port, exc
                )

        if not transports:
            _LOGGER.debug("Tylo Sauna discovery(user): no UDP sockets opened")
            return []

        try:
            await asyncio.sleep(UDP_DISCOVERY_TIMEOUT)
        finally:
            for t in transports:
                t.close()

        devices = list(found.values())

        # Filter out saunas that already have a config entry
        existing_entries = hass.config_entries.async_entries(DOMAIN)
        known_guids = {e.data.get("guid") for e in existing_entries if e.data.get("guid")}
        known_hosts = {e.data.get("host") for e in existing_entries if e.data.get("host")}

        filtered = [
            s for s in devices
            if s.guid not in known_guids and s.host not in known_hosts
        ]

        _LOGGER.debug(
            "Tylo Sauna discovery(user): found %d new, %d total, %d filtered out",
            len(filtered), len(devices), len(devices) - len(filtered),
        )

        return filtered

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        """
        First step:
        - Try discovery.
        - If something is found, show a list + manual IP option.
        - Otherwise show manual host/port/name form.
        """
        errors: dict[str, str] = {}

        if user_input is not None:
            relaxed = user_input.get("relaxed_telemetry", True)

            # Device selected from discovery list
            if "device" in user_input and user_input["device"] != "__manual__":
                guid = user_input["device"]
                sauna = self._discovered.get(guid)
                if not sauna:
                    errors["base"] = "device_not_found"
                else:
                    host = sauna.host
                    name = user_input.get("name") or f"Tylo Sauna {host}"

                    await self.async_set_unique_id(sauna.guid)
                    self._abort_if_unique_id_configured()

                    data = {
                        "host": host,
                        "port": 42156,
                        "name": name,
                        "guid": sauna.guid,
                        "relaxed_telemetry": relaxed,
                    }
                    return self.async_create_entry(title=name, data=data)

            # Manual host entry
            if "host" in user_input and user_input["host"]:
                host = user_input["host"]
                port = user_input.get("port", 42156)
                name = user_input.get("name") or f"Tylo Sauna {host}"

                await self.async_set_unique_id(host)
                self._abort_if_unique_id_configured()

                data = {
                    "host": host,
                    "port": port,
                    "name": name,
                    "relaxed_telemetry": relaxed,
                }
                return self.async_create_entry(title=name, data=data)

        # No user input yet – run discovery
        if not self._discovered:
            self._discovered = {
                s.guid: s
                for s in await self._async_discover(self.hass)
            }

        # If discovery found something – show the list
        if self._discovered:
            options = {
                guid: f"{sauna.host} ({guid})"
                for guid, sauna in self._discovered.items()
            }
            options["__manual__"] = "Enter IP manually"

            schema = vol.Schema(
                {
                    vol.Required("device", default=list(options.keys())[0]): vol.In(options),
                    vol.Optional("host"): str,
                    vol.Optional("port", default=42156): int,
                    vol.Optional("name", default="Tylo Sauna"): str,
                    vol.Optional("relaxed_telemetry", default=True): bool,
                }
            )
            return self.async_show_form(
                step_id="user",
                data_schema=schema,
                errors=errors,
            )

        # Nothing discovered – fall back to manual configuration
        schema = vol.Schema(
            {
                vol.Required("host"): str,
                vol.Optional("port", default=42156): int,
                vol.Optional("name", default="Tylo Sauna"): str,
                vol.Optional("relaxed_telemetry", default=True): bool,
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)
