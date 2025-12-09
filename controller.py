import asyncio
import logging

_LOGGER = logging.getLogger(__name__)

KEEPALIVE_INTERVAL = 15  # seconds, matches official app behavior

# HELLO / INIT packets reverse engineered from the original app
HELLO_PAYLOAD = bytes.fromhex(
    "c23e33081412043030303028542879286c28f601282028722865286d286f28"
    "74286528202863286f286e28742872286f286c3a025001"
)
INIT_SHORT = bytes.fromhex("8241020802")

# Light commands
LIGHT_OFF_PAYLOAD = bytes.fromhex("a24204080a1000")
LIGHT_ON_PAYLOAD  = bytes.fromhex("a24204080a1001")

# Heating commands
HEAT_ON_PAYLOAD  = bytes.fromhex("c24302500b")
HEAT_OFF_PAYLOAD = bytes.fromhex("c24302500a")
HEAT_AUX_PAYLOAD = bytes.fromhex("d23e02081f")  # extra packet sent by the app for HEAT


def _decode_varint(data: bytes, start: int):
    """
    Simple protobuf varint decoder.
    Returns (value, next_index) or (None, start) if incomplete.
    """
    result = 0
    shift = 0
    i = start
    while i < len(data):
        b = data[i]
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            return result, i + 1
        shift += 7
        i += 1
    return None, start


def _encode_varint(value: int) -> bytes:
    """Encode an integer as protobuf varint."""
    out = bytearray()
    v = int(value)
    if v < 0:
        raise ValueError("varint only supports non-negative integers")
    while True:
        b = v & 0x7F
        v >>= 7
        if v:
            out.append(b | 0x80)
        else:
            out.append(b)
            break
    return bytes(out)


def _parse_varint_after(data: bytes, pattern_hex: str):
    """Find varint immediately after a given hex pattern."""
    pattern = bytes.fromhex(pattern_hex)
    idx = data.find(pattern)
    if idx == -1:
        return None
    val, _ = _decode_varint(data, idx + len(pattern))
    return val


class SaunaProtocol(asyncio.DatagramProtocol):
    """Asyncio protocol used by SaunaController."""

    def __init__(self, controller: "SaunaController"):
        self.controller = controller
        self.transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = transport  # type: ignore[assignment]
        self.controller.connection_made(self.transport)  # type: ignore[arg-type]

    def datagram_received(self, data: bytes, addr) -> None:
        self.controller.datagram_received(data, addr)

    def error_received(self, exc: Exception) -> None:
        _LOGGER.warning("Tylo Sauna UDP error: %s", exc)

    def connection_lost(self, exc: Exception | None) -> None:
        _LOGGER.info("Tylo Sauna UDP connection lost: %s", exc)
        self.controller.connection_lost(exc)


class SaunaController:
    """Encapsulates the local UDP protocol for Tylo Elite sauna."""

    def __init__(self, hass, host: str, port: int, name: str) -> None:
        self._hass = hass
        self.host = host
        self.port = port
        self.name = name

        self._transport: asyncio.DatagramTransport | None = None
        self._protocol: SaunaProtocol | None = None
        self._keepalive_task: asyncio.Task | None = None

        # Sauna state (mirrored from telemetry)
        self.light: bool | None = None
        self.heat: bool | None = None
        self.t_set_c: float | None = None
        self.t_cur_c: float | None = None
        self.stop_cfg_min: int | None = None   # configured Stop after (minutes)
        self.stop_rem_min: int | None = None   # remaining time until auto-off (minutes)

        # Entity callbacks (climate, light, number)
        self._callbacks: list[callable] = []

    async def async_start(self) -> None:
        """
        Create UDP socket and send initial HELLO/INIT sequence.
        Keepalive loop is started separately via async_start_keepalive().
        """
        loop = self._hass.loop
        _LOGGER.info("Tylo Sauna: creating UDP endpoint for %s:%s", self.host, self.port)

        self._transport, self._protocol = await loop.create_datagram_endpoint(
            lambda: SaunaProtocol(self),
            local_addr=("0.0.0.0", 0),
        )

        # Run HELLO/INIT in the background
        self._hass.create_task(self._async_init_sequence())

    async def _async_init_sequence(self) -> None:
        """Initial HELLO + INIT sequence when the controller starts."""
        await asyncio.sleep(0.5)
        self._send(HELLO_PAYLOAD, "HELLO 1")
        await asyncio.sleep(0.1)
        self._send(HELLO_PAYLOAD, "HELLO 2")
        await asyncio.sleep(0.1)
        self._send(HELLO_PAYLOAD, "HELLO 3")
        await asyncio.sleep(0.1)
        self._send(INIT_SHORT, "INIT_SHORT")

    async def _keepalive_loop(self) -> None:
        """Background keepalive loop (INIT_SHORT every KEEPALIVE_INTERVAL)."""
        try:
            while True:
                await asyncio.sleep(KEEPALIVE_INTERVAL)
                self._send(INIT_SHORT, "KEEPALIVE")
        except asyncio.CancelledError:
            _LOGGER.info("Tylo Sauna: keepalive loop cancelled")
            raise

    async def async_start_keepalive(self) -> None:
        """
        Start keepalive loop.
        This should be called only after Home Assistant has fully started.
        """
        if self._keepalive_task is not None and not self._keepalive_task.done():
            return
        _LOGGER.info("Tylo Sauna: starting keepalive loop")
        self._keepalive_task = self._hass.create_task(self._keepalive_loop())

    # === Network events ===

    def _send(self, payload: bytes, desc: str = "") -> None:
        if not self._transport:
            _LOGGER.warning(
                "Tylo Sauna: transport not ready, cannot send %s", desc or ""
            )
            return
        self._transport.sendto(payload, (self.host, self.port))
        if desc:
            _LOGGER.debug("Tylo Sauna: send %s (%d bytes)", desc, len(payload))

    def connection_made(self, transport: asyncio.DatagramTransport) -> None:
        sockname = transport.get_extra_info("sockname")
        _LOGGER.info("Tylo Sauna: UDP socket bound on %s", sockname)

    def connection_lost(self, exc: Exception | None) -> None:
        _LOGGER.info("Tylo Sauna: connection lost: %s", exc)

    def datagram_received(self, data: bytes, addr) -> None:
        # Filter out packets not coming from the sauna
        if addr[0] != self.host:
            return
        self._handle_telemetry(data)

    # === Telemetry parsing ===

    def _handle_telemetry(self, data: bytes) -> None:
        """Parse incoming telemetry and update internal state."""
        changed = False

        # Light
        light = self._parse_light(data)
        if light is not None and light != self.light:
            self.light = light
            changed = True

        # Stop-after configuration (minutes)
        stop_cfg = self._parse_stop_cfg(data)
        if stop_cfg is not None and stop_cfg != self.stop_cfg_min:
            self.stop_cfg_min = stop_cfg
            changed = True

        # Remaining time to auto-off (minutes)
        stop_rem = self._parse_stop_rem(data)
        if stop_rem is not None and stop_rem != self.stop_rem_min:
            self.stop_rem_min = stop_rem
            changed = True

        # Heating flag: we consider heating ON if there is remaining time
        new_heat = None
        if self.stop_rem_min is not None:
            new_heat = self.stop_rem_min > 0
        if new_heat is not None and new_heat != self.heat:
            self.heat = new_heat
            changed = True

        # Temperature setpoint
        t_set_c = self._parse_temp_set(data)
        if t_set_c is not None and t_set_c != self.t_set_c:
            self.t_set_c = t_set_c
            changed = True

        # Current temperature
        t_cur_c = self._parse_temp_cur(data)
        if t_cur_c is not None and t_cur_c != self.t_cur_c:
            self.t_cur_c = t_cur_c
            changed = True

        if changed:
            _LOGGER.info(
                "Tylo Sauna state: LIGHT=%s, HEAT=%s, Tset=%s°C, Tcur=%s°C, StopCfg=%s, StopRem=%s",
                self.light,
                self.heat,
                f"{self.t_set_c:.1f}" if self.t_set_c is not None else "?",
                f"{self.t_cur_c:.1f}" if self.t_cur_c is not None else "?",
                self.stop_cfg_min if self.stop_cfg_min is not None else "?",
                self.stop_rem_min if self.stop_rem_min is not None else "?",
            )
            self._notify_listeners()

    def _parse_light(self, data: bytes) -> bool | None:
        """Light flag: pattern da 7d 04 08 0a 10 XX (XX = 0/1)."""
        pattern = bytes.fromhex("da7d04080a10")
        idx = data.find(pattern)
        if idx == -1 or idx + len(pattern) >= len(data):
            return None
        val = data[idx + len(pattern)]
        if val == 1:
            return True
        if val == 0:
            return False
        return None

    def _parse_stop_cfg(self, data: bytes) -> int | None:
        """
        Stop-after configuration (minutes) – field 0x11:

          d2 7d 05 08 11 10 <varint>
        or
          d2 7d 04 08 11 10 <varint>
        """
        for prefix_hex in ("d27d05081110", "d27d04081110"):
            val = _parse_varint_after(data, prefix_hex)
            if val is not None:
                return val
        return None

    def _parse_stop_rem(self, data: bytes) -> int | None:
        """
        Remaining time to auto-off (minutes) – field 0x16:

          d2 7d 05 08 16 10 <varint>
        or
          d2 7d 04 08 16 10 <varint>
        """
        for prefix_hex in ("d27d05081610", "d27d04081610"):
            val = _parse_varint_after(data, prefix_hex)
            if val is not None:
                return val
        return None

    def _parse_temp_set(self, data: bytes) -> float | None:
        """Temperature setpoint: varint after d2 7d 05 08 0a 10, raw = °C * 9."""
        raw = _parse_varint_after(data, "d27d05080a10")
        if raw is None:
            return None
        return raw / 9.0

    def _parse_temp_cur(self, data: bytes) -> float | None:
        """Current temperature: varint after d2 7d 05 08 0c 10, raw = °C * 9."""
        raw = _parse_varint_after(data, "d27d05080c10")
        if raw is None:
            return None
        return raw / 9.0

    # === API for entities (climate, light, number) ===

    def register_callback(self, cb) -> None:
        """Register a callback (typically entity.async_write_ha_state)."""
        self._callbacks.append(cb)

    def _notify_listeners(self) -> None:
        for cb in list(self._callbacks):
            try:
                cb()
            except Exception as exc:  # noqa: BLE001
                _LOGGER.exception("Tylo Sauna callback error: %s", exc)

    # --- Commands ---

    def light_on(self) -> None:
        """Turn sauna light on."""
        self._send(LIGHT_ON_PAYLOAD, "LIGHT ON")

    def light_off(self) -> None:
        """Turn sauna light off."""
        self._send(LIGHT_OFF_PAYLOAD, "LIGHT OFF")

    def heat_on(self) -> None:
        """Turn sauna heating on."""
        self._send(HEAT_ON_PAYLOAD, "HEAT ON")
        self._send(HEAT_AUX_PAYLOAD, "HEAT AUX")

    def heat_off(self) -> None:
        """Turn sauna heating off."""
        self._send(HEAT_OFF_PAYLOAD, "HEAT OFF")
        self._send(HEAT_AUX_PAYLOAD, "HEAT AUX")

    async def async_set_temperature(self, temp_c: float) -> None:
        """Set target temperature (°C)."""
        raw = int(round(temp_c * 9.0))
        prefix = bytes.fromhex("d24105080a10")
        payload = prefix + _encode_varint(raw)
        self._send(payload, f"SETTEMP {temp_c:.1f}°C")

    async def async_set_stop_after(self, minutes: int) -> None:
        """
        Set Stop after (auto-off timer configuration) in minutes.

        The official app sends two packets:

          1) d2 41 05 08 0e 10 <varint(minutes)>
          2) d2 3e 02 08 01
        """
        m = int(minutes)
        var = _encode_varint(m)
        p1 = bytes.fromhex("d24105080e10") + var
        p2 = bytes.fromhex("d23e020801")
        self._send(p1, f"SETSTOP {m} min (cfg)")
        await asyncio.sleep(0.02)
        self._send(p2, "SETSTOP aux")
