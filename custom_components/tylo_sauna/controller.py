import asyncio
from contextlib import suppress
import logging
import re
from collections import deque
from dataclasses import dataclass
from typing import Any
import time
from homeassistant.util import dt as dt_util
from datetime import timedelta
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.exceptions import HomeAssistantError  # type: ignore[import-not-found]

from .const import (
    DEFAULT_CONTROL_PORT,
    DOMAIN,
    KEEPALIVE_INTERVAL,
    ONLINE_TIMEOUT_S,
    UDP_DISCOVERY_PORTS,
)


_LOGGER = logging.getLogger(__name__)

# HELLO / INIT packets reverse engineered from the official app
HELLO_PAYLOAD = bytes.fromhex(
    "c23e33081412043030303028542879286c28f601282028722865286d286f28"
    "74286528202863286f286e28742872286f286c3a025001"
)
INIT_SHORT = bytes.fromhex("8241020802")
CONTROL_SESSION_REFRESH_MIN_INTERVAL = 5.0

# Light commands
LIGHT_OFF_PAYLOAD = bytes.fromhex("a24204080a1000")
LIGHT_ON_PAYLOAD  = bytes.fromhex("a24204080a1001")

# Heating commands
HEAT_ON_PAYLOAD  = bytes.fromhex("c24302500b")
HEAT_OFF_PAYLOAD = bytes.fromhex("c24302500a")
STANDBY_PAYLOAD  = bytes.fromhex("c24302500c")  # standby mode (reduced temperature)
HEAT_AUX_PAYLOAD = bytes.fromhex("d23e02081f")  # extra packet sent by the app for HEAT

# Operating modes (from telemetry response 9280010258XX)
MODE_OFF = 0x0a
MODE_HEAT = 0x0b
MODE_STANDBY = 0x0c

# --- Steam/Aroma (экспериментально) ---
# Наблюдалось на steam-контроллере с ароматизацией (Eucalyptus) в `Steam verde*.pcapng`.
# Важно: семантика поля `22 00/01` пока не подтверждена на 100%, поэтому делаем две отдельные кнопки ON/OFF.
AROMA_EUCALYPTUS_OFF = bytes.fromhex(
    "92441a0800100c184718651875187220002a04500b583c3204500a5805"
)
AROMA_EUCALYPTUS_ON = bytes.fromhex(
    "92441a0800100c184718651875187220012a04500b583c3204500a5805"
)

# Echo/response от контроллера после aroma-команды (protobuf field ~2070)
AROMA_EVENT_PREFIX = bytes.fromhex("b28101")

UUID_RE = re.compile(
    rb"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)


def _decode_varint(data: bytes, start: int):
    """Simple protobuf varint decoder."""
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


def _extract_guid_from_payload(data: bytes) -> str | None:
    """Try to extract a GUID/UUID from payload as a hint."""
    m = UUID_RE.search(data)
    if not m:
        return None
    try:
        return m.group(0).decode("ascii")
    except Exception:  # noqa: BLE001
        return None


# Favorites / presets (Favourites in the Tylo app)
FAVORITES_REQ = bytes.fromhex("d23e03089303")  # request favorites list (app uses request_id=403)
FAVORITES_SNAPSHOT_FIELD = 2040  # c2 7f

# Fault events (door cancel etc.)
FAULT_EVENT_FIELD = 2110  # f2 83 01
FAULT_ACK_FIELD = 1120    # 82 46

DOOR_FAULT_CODES = {19, 20}
STATE_PENDING_ACK = 13
STATE_ACKED = 10

TEMP_SCALE = 9  # protocol uses temp_c * 9

# Telemetry mappings (KV id -> meaning)
# Note: the same KV id can mean different things in different message types.
STATUS_KV_MAP: dict[int, tuple[str, callable]] = {
    0x0A: ("t_set_c", lambda v: float(v) / TEMP_SCALE),
    0x0C: ("t_cur_c", lambda v: float(v) / TEMP_SCALE),
    0x11: ("stop_cfg_min", int),
    0x16: ("stop_rem_min", int),
    0x13: ("humidity_cur_pct", int),   # current humidity % (Combi/Steam)
    0x14: ("humidity_set_pct", int),   # humidity setpoint % (Combi/Steam)
}

FLAGS_KV_MAP: dict[int, tuple[str, callable]] = {
    0x0A: ("light", lambda v: bool(int(v))),
}


@dataclass(frozen=True)
class Favorite:
    slot: int
    enabled: bool
    name: str = ""
    target_temp_c: float | None = None
    stop_after_min: int | None = None
    light_on: bool | None = None


# Schedule / Programs (calendar tab in the official app)
SCHEDULE_SLOT_FIELD = 2045  # ea 7f
Y2K_EPOCH_OFFSET = 946684800  # seconds from Unix epoch to 2000-01-01 00:00:00 UTC


@dataclass(frozen=True)
class ScheduleEntry:
    slot: int
    enabled: bool
    ready_at_utc: float | None = None  # Unix timestamp (seconds)
    stop_after_min: int | None = None
    temp_c: float | None = None
    mode: int = 0  # 0=Bath, 1=Standby
    favorite_index: int | None = None  # None=Custom, int=Favorite slot


@dataclass(frozen=True)
class FaultEvent:
    code: int
    state: int
    detail: int | None
    message: str
    message_bytes: bytes


def _pb_iter_fields(buf: bytes):
    """Iterate protobuf fields: yields (field_no, wire_type, value)."""
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
            raw = buf[i : i + 4]
            i += 4
            yield field_no, wt, raw
        elif wt == 1:  # 64-bit
            raw = buf[i : i + 8]
            i += 8
            yield field_no, wt, raw
        else:
            # Unsupported
            return


def _pb_collect(buf: bytes) -> dict[int, list[tuple[int, Any]]]:
    out: dict[int, list[tuple[int, Any]]] = {}
    for f, wt, v in _pb_iter_fields(buf):
        out.setdefault(f, []).append((wt, v))
    return out


def _pb_first_varint(msg: dict[int, list[tuple[int, Any]]], n: int) -> int | None:
    for wt, v in msg.get(n, []):
        if wt == 0:
            return int(v)
    return None


def _pb_all_varints(msg: dict[int, list[tuple[int, Any]]], n: int) -> list[int]:
    res: list[int] = []
    for wt, v in msg.get(n, []):
        if wt == 0:
            res.append(int(v))
    return res


def _collect_kv_pairs(buf: bytes, *, max_depth: int = 6) -> list[tuple[int, int]]:
    """Recursively collect KV-like pairs from protobuf-like messages.

    Many Tylo messages contain repeated tiny submessages like:
      field 1 (varint) -> key/id
      field 2 (varint) -> value
    wrapped in various containers.
    """
    if max_depth <= 0 or not buf:
        return []

    pairs: list[tuple[int, int]] = []
    try:
        msg = _pb_collect(buf)
    except Exception:  # noqa: BLE001
        return []

    # Only treat a message as a KV leaf if it is *exactly* {1: varint(id), 2: varint(value)}.
    # This avoids accidental matches in wrapper messages where fields 1/2 mean something else.
    try:
        if set(msg.keys()) == {1, 2} and len(msg.get(1, [])) == 1 and len(msg.get(2, [])) == 1:
            (wt1, v1) = msg[1][0]
            (wt2, v2) = msg[2][0]
            if wt1 == 0 and wt2 == 0:
                pairs.append((int(v1), int(v2)))
    except Exception:  # noqa: BLE001
        pass

    # Recurse into all length-delimited fields
    for _field_no, items in msg.items():
        for wt, val in items:
            if wt != 2:
                continue
            if not isinstance(val, (bytes, bytearray)):
                continue
            pairs.extend(_collect_kv_pairs(bytes(val), max_depth=max_depth - 1))

    return pairs


def _extract_mapped_fields(data: bytes, mapping: dict[int, tuple[str, callable]]) -> dict[str, Any]:
    """Extract mapped fields from a telemetry payload using KV pair extraction."""
    out: dict[str, Any] = {}
    for kid, raw in _collect_kv_pairs(data):
        if kid not in mapping:
            continue
        name, conv = mapping[kid]
        try:
            # Prefer the latest value within the packet (overwrite on repeats).
            out[name] = conv(raw)
        except Exception:  # noqa: BLE001
            continue
    return out


def parse_favorites_snapshot(payload: bytes) -> dict[int, Favorite]:
    """Parse favorites snapshot (field 2040, c2 7f...)."""
    top = _pb_collect(payload)
    out: dict[int, Favorite] = {}

    for wt, raw in top.get(FAVORITES_SNAPSHOT_FIELD, []):
        if wt != 2:
            continue
        entry = _pb_collect(raw)
        slot = _pb_first_varint(entry, 1)
        enabled = _pb_first_varint(entry, 2)

        if slot is None:
            continue

        if enabled != 1:
            out[int(slot)] = Favorite(slot=int(slot), enabled=False)
            continue

        name = bytes(_pb_all_varints(entry, 3)).decode("utf-8", errors="replace")
        temp_scaled = _pb_first_varint(entry, 5)
        stop_after = _pb_first_varint(entry, 6)
        light = _pb_first_varint(entry, 7)

        out[int(slot)] = Favorite(
            slot=int(slot),
            enabled=True,
            name=name,
            target_temp_c=(float(temp_scaled) / TEMP_SCALE) if temp_scaled is not None else None,
            stop_after_min=int(stop_after) if stop_after is not None else None,
            light_on=(bool(light) if light is not None else None),
        )

    return out


def parse_fault_event(payload: bytes) -> FaultEvent | None:
    """Parse fault event (field 2110, f2 83 01...)."""
    top = _pb_collect(payload)
    items = top.get(FAULT_EVENT_FIELD, [])
    if not items:
        return None

    wt, raw = items[0]
    if wt != 2:
        return None

    msg = _pb_collect(raw)
    code = _pb_first_varint(msg, 1)
    state = _pb_first_varint(msg, 2)
    detail = _pb_first_varint(msg, 3)

    if code is None or state is None:
        return None

    msg_bytes = bytes(_pb_all_varints(msg, 5))
    message = msg_bytes.decode("utf-8", errors="replace")

    return FaultEvent(
        code=int(code),
        state=int(state),
        detail=int(detail) if detail is not None else None,
        message=message,
        message_bytes=msg_bytes,
    )


def parse_schedule_slots(payload: bytes) -> list[ScheduleEntry]:
    """Parse schedule/programs from telemetry (field 2045 repeated sub-messages).

    Field 2045 is nested inside the da7d envelope (field 2011), so we
    check both the top level and one level down.
    """
    top = _pb_collect(payload)
    entries: list[ScheduleEntry] = []

    # Field 2045 may be at top level or nested inside field 2011 (da7d envelope)
    slot_items = top.get(SCHEDULE_SLOT_FIELD, [])
    if not slot_items:
        for wt_outer, body in top.get(2011, []):
            if wt_outer != 2:
                continue
            inner = _pb_collect(body)
            slot_items = inner.get(SCHEDULE_SLOT_FIELD, [])
            if slot_items:
                break

    for wt, raw in slot_items:
        if wt != 2:
            continue
        msg = _pb_collect(raw)
        slot = _pb_first_varint(msg, 1)
        enabled = _pb_first_varint(msg, 2)
        if slot is None:
            continue
        if enabled != 1:
            continue

        ts_raw = _pb_first_varint(msg, 3)  # seconds from Y2K epoch ×1000
        ready_at = (float(ts_raw) / 1000.0 + Y2K_EPOCH_OFFSET) if ts_raw else None
        stop_min = _pb_first_varint(msg, 4)
        temp_raw = _pb_first_varint(msg, 6)  # °C × TEMP_SCALE
        temp_c = (float(temp_raw) / TEMP_SCALE) if temp_raw is not None else None
        mode = _pb_first_varint(msg, 9) or 0
        fav_idx = _pb_first_varint(msg, 10)  # present only for Favorite entries

        entries.append(ScheduleEntry(
            slot=int(slot),
            enabled=True,
            ready_at_utc=ready_at,
            stop_after_min=int(stop_min) if stop_min is not None else None,
            temp_c=temp_c,
            mode=mode,
            favorite_index=fav_idx,
        ))

    entries.sort(key=lambda e: e.ready_at_utc or 0)
    return entries


def _pb_encode_key(field_no: int, wire_type: int) -> bytes:
    return _encode_varint((int(field_no) << 3) | int(wire_type))


def _pb_encode_varint_field(field_no: int, value: int) -> bytes:
    return _pb_encode_key(field_no, 0) + _encode_varint(int(value))


def _pb_encode_bytes_field(field_no: int, raw: bytes) -> bytes:
    return _pb_encode_key(field_no, 2) + _encode_varint(len(raw)) + raw


def encode_fault_ack(fault: FaultEvent) -> bytes:
    """Encode ACK packet like the official app (field1120)."""
    body = bytearray()
    body += _pb_encode_varint_field(1, fault.code)
    body += _pb_encode_varint_field(2, STATE_PENDING_ACK)  # app uses 13 in ack request
    body += _pb_encode_varint_field(3, 21)                 # observed in app ack
    for ch in (fault.message_bytes or fault.message.encode("utf-8", errors="ignore")):
        body += _pb_encode_varint_field(5, int(ch))
    body += _pb_encode_varint_field(6, 0)
    return _pb_encode_bytes_field(FAULT_ACK_FIELD, bytes(body))


def _looks_like_tylo_telemetry(data: bytes) -> bool:
    """
    Heuristic check to avoid accepting random UDP noise as telemetry.
    """
    # Allow known non-telemetry packets that we still want to accept
    if data.startswith(b"\xc2\x7f"):  # favorites snapshot (field 2040)
        return True
    if data.startswith(b"\xf2\x83\x01"):  # fault event (field 2110)
        return True
    if data.startswith(b"\xea\x7f"):  # schedule slot (field 2045)
        return True
    if data.startswith(b"\xf2\x7e"):  # schedule notification (field 2030)
        return True

    # Known telemetry families (KV status + flags)
    if data.startswith(b"\xd2\x7d") or data.startswith(b"\xda\x7d"):
        return True

    # Legacy markers (very reliable for sauna-only firmwares)
    markers = (
        b"\xd2\x7d\x05\x08\x0a\x10",  # Tset
        b"\xd2\x7d\x05\x08\x0c\x10",  # Tcur
        b"\xd2\x7d\x04\x08\x11\x10",  # StopCfg alt
        b"\xd2\x7d\x05\x08\x11\x10",  # StopCfg
        b"\xd2\x7d\x04\x08\x16\x10",  # StopRem alt
        b"\xd2\x7d\x05\x08\x16\x10",  # StopRem
        b"\xda\x7d\x04\x08\x0a\x10",  # Light flag
    )
    if any(m in data for m in markers):
        return True

    # Fallback: generic extractor (best-effort)
    return bool(_extract_mapped_fields(data, STATUS_KV_MAP) or _extract_mapped_fields(data, FLAGS_KV_MAP))


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
        self.controller.connection_lost(exc, self.transport)


class SaunaController:
    """
    Local UDP controller for Tylo Elite.

    The official app sends HELLO 3 times, INIT_SHORT, then periodic KEEPALIVE (same INIT_SHORT).
    Telemetry packets are received asynchronously.
    """

    def __init__(
        self,
        hass,
        name: str,
        host: str,
        port: int = 54377,
        guid: str | None = None,
        experimental_aroma: bool = False,
        debug_recording: bool = False,
    ):
        self._hass = hass
        self.name = name
        self.host = host
        self.port = port
        # Configured port (from UI). Actual control port may be learned from src_port.
        self.configured_port = int(port)
        self.control_port = int(port)
        # Last seen telemetry (for online/last_seen sensors)
        self.last_rx_dt = None  # datetime in UTC

        self.last_rx_ip: str | None = None
        self.last_rx_port: int | None = None

        self._unsub_watchdog = None
        self._unsub_keepalive = None
        self._online_cached = False
        self._last_publish_monotonic = time.monotonic()
        self._last_offline_probe_monotonic = 0.0

        # If user configured a discovery port, also probe the legacy observed control-port candidate.
        self._probe_ports: set[int] = {self.control_port}
        if self.control_port in UDP_DISCOVERY_PORTS:
            self._probe_ports |= {DEFAULT_CONTROL_PORT, *UDP_DISCOVERY_PORTS}

        self.guid = guid
        # Экспериментальные функции включаются только явно через OptionsFlow.
        self.experimental_aroma = bool(experimental_aroma)
        self.debug_recording = bool(debug_recording)
        self._debug_buffer: deque[dict] = deque(maxlen=2000)
        self.endpoint_source: str = "config"

        self._transport: asyncio.DatagramTransport | None = None
        self._protocol: SaunaProtocol | None = None
        self._stopping = False
        self._reconnect_task: asyncio.Task | None = None
        self._init_task: asyncio.Task | None = None
        self._control_session_lock = asyncio.Lock()
        self._last_control_session_refresh_monotonic: float | None = None

        # Learned telemetry sender (may differ from configured host)
        self.telemetry_host: str | None = None

        # Sauna state (mirrored from telemetry)
        self.light: bool | None = None
        self.heat: bool | None = None
        self.t_set_c: float | None = None
        self.t_cur_c: float | None = None
        self.stop_cfg_min: int | None = None   # configured Stop after (minutes)
        self.stop_rem_min: int | None = None   # remaining time to auto-off (minutes)
        self.humidity_cur_pct: int | None = None  # current humidity % (Combi/Steam)
        self.humidity_set_pct: int | None = None  # humidity setpoint % (Combi/Steam)

        # Faults / safety events
        self.last_fault: FaultEvent | None = None
        self.door_fault_pending: bool = False

        # Standby mode
        self.current_mode: int | None = None  # MODE_OFF/MODE_HEAT/MODE_STANDBY
        self.standby_enabled: bool = False    # standby available in settings
        self.standby_delta_c: float | None = None  # temperature reduction in standby

        # Favorites / presets
        self.favorites: dict[int, Favorite] = {}
        self.last_selected_favorite_slot: int | None = None

        # Schedule / programs
        self.schedule: list[ScheduleEntry] = []

        # Diagnostics
        self.rx_packets = 0
        self.tx_packets = 0
        self.last_rx_monotonic: float | None = None

        # --- Aroma diagnostics (экспериментально) ---
        self.last_aroma_name: str | None = None
        self.last_aroma_flag: int | None = None
        self.last_aroma_dt = None  # datetime in UTC

        # Callbacks to notify entities
        self._callbacks: list[callable] = []

    def maybe_update_host(self, host: str, source: str = "unknown", guid: str | None = None) -> None:
        """Update effective host when we learn a better value (announce/cache)."""
        new_host = str(host).strip()
        if not new_host:
            return

        # Never switch the effective endpoint to loopback/unspecified addresses.
        # In some Docker/macOS setups the sauna announce may appear to come from 127.0.0.1,
        # but that is not a routable endpoint for UDP control.
        try:
            import ipaddress

            ip = ipaddress.ip_address(new_host)
            if ip.is_loopback or ip.is_unspecified:
                return
        except Exception:  # noqa: BLE001
            # If parsing fails, be conservative: only reject obvious loopback literals.
            if new_host in {"127.0.0.1", "0.0.0.0", "::1", "::"}:
                return

        if str(getattr(self, "host", "")).strip() == new_host:
            return

        old = str(getattr(self, "host", "")).strip()
        self.host = new_host
        self.endpoint_source = str(source)

        # Reset pinned telemetry host so we can accept packets from the new endpoint.
        self.telemetry_host = None
        self.last_rx_ip = None
        self.last_rx_port = None
        self.last_rx_monotonic = None

        _LOGGER.info(
            "Tylo Sauna: updated host=%s (was %s) via %s (guid=%s)",
            self.host,
            old,
            source,
            guid or getattr(self, "guid", None) or "n/a",
        )

    def maybe_update_control_port(self, port: int, source: str = "unknown", src_ip: str | None = None, guid: str | None = None) -> None:
        """Update effective control_port when we learn a better value (announce/telemetry)."""
        try:
            new_port = int(port)
        except Exception:  # noqa: BLE001
            return

        if new_port <= 0 or new_port > 65535:
            return

        if new_port == int(getattr(self, "control_port", 0)):
            return

        old = int(getattr(self, "control_port", 0))
        self.control_port = new_port
        self._probe_ports = {self.control_port}
        self.endpoint_source = str(source)
        _LOGGER.info(
            "Tylo Sauna: updated control_port=%s (was %s) via %s from %s (guid=%s)",
            self.control_port,
            old,
            source,
            src_ip or "n/a",
            guid or getattr(self, "guid", None) or "n/a",
        )

        # Fast recovery: after port change some firmwares require a fresh HELLO/INIT handshake
        # before accepting commands on the new port (telemetry may still arrive).
        if self._transport:
            try:
                self._hass.async_create_task(self._async_port_switch_handshake(int(self.control_port)))
            except Exception:  # noqa: BLE001
                # fallback: at least send INIT once
                try:
                    self._send(INIT_SHORT, desc="PORT_SWITCH_INIT", port=int(self.control_port))
                except Exception:  # noqa: BLE001
                    pass

    async def _async_port_switch_handshake(self, port: int) -> None:
        """Send a short HELLO/INIT sequence to the specified port to re-establish control."""
        try:
            p = int(port)
        except Exception:  # noqa: BLE001
            return
        if not (0 < p <= 65535):
            return
        # Reuse the same pattern as the initial sequence (small delays are intentional).
        self._send(HELLO_PAYLOAD, "PORT_SWITCH_HELLO 1", port=p)
        await asyncio.sleep(0.1)
        self._send(HELLO_PAYLOAD, "PORT_SWITCH_HELLO 2", port=p)
        await asyncio.sleep(0.1)
        self._send(HELLO_PAYLOAD, "PORT_SWITCH_HELLO 3", port=p)
        await asyncio.sleep(0.1)
        self._send(INIT_SHORT, "PORT_SWITCH_INIT", port=p)
        if p == int(self.control_port):
            self._last_control_session_refresh_monotonic = time.monotonic()

    async def async_start(self) -> None:
        """Create UDP socket and send initial HELLO/INIT sequence."""
        self._stopping = False
        await self._async_open_transport()

    async def _async_open_transport(self) -> None:
        """Create the UDP endpoint and initialize its controller session."""
        loop = self._hass.loop
        _LOGGER.info("Tylo Sauna: creating UDP endpoint for %s:%s", self.host, self.port)

        transport, protocol = await loop.create_datagram_endpoint(
            lambda: SaunaProtocol(self),
            local_addr=("0.0.0.0", 0),
        )
        if self._stopping:
            if self._transport is transport:
                self._transport = None
            self._protocol = None
            transport.close()
            return
        self._transport = transport
        self._protocol = protocol
        self.start_watchdog()
        init_task = self._hass.async_create_task(self._async_init_sequence())
        self._init_task = init_task
        init_task.add_done_callback(self._init_finished)

    def _init_finished(self, task: asyncio.Task) -> None:
        if self._init_task is task:
            self._init_task = None

    async def _async_reconnect(self) -> None:
        """Recreate a lost local UDP endpoint with bounded backoff."""
        delay = 1.0
        try:
            while not self._stopping and self._transport is None:
                await asyncio.sleep(delay)
                try:
                    await self._async_open_transport()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    _LOGGER.warning("Tylo Sauna: UDP reconnect failed: %s", exc)
                    delay = min(delay * 2.0, 30.0)
                else:
                    _LOGGER.info("Tylo Sauna: UDP transport reconnected")
                    return
        finally:
            self._reconnect_task = None

    def _schedule_reconnect(self) -> None:
        if self._stopping:
            return
        if self._reconnect_task is not None and not self._reconnect_task.done():
            return
        self._reconnect_task = self._hass.async_create_task(self._async_reconnect())

    async def _async_init_sequence(self) -> None:
        self._send_probe(HELLO_PAYLOAD, "HELLO 1")
        await asyncio.sleep(0.1)
        self._send_probe(HELLO_PAYLOAD, "HELLO 2")
        await asyncio.sleep(0.1)
        self._send_probe(HELLO_PAYLOAD, "HELLO 3")
        await asyncio.sleep(0.1)
        self._send_probe(INIT_SHORT, "INIT_SHORT")
        self._last_control_session_refresh_monotonic = time.monotonic()
        await asyncio.sleep(0.1)
        self.request_favorites()

    async def async_prepare_control_session(self) -> bool:
        """Refresh the controller handshake immediately before an actuation burst."""
        async with self._control_session_lock:
            transport = self._transport
            if transport is None or transport.is_closing():
                _LOGGER.warning("Tylo Sauna: cannot refresh control session without UDP transport")
                return False

            now_m = time.monotonic()
            last = self._last_control_session_refresh_monotonic
            if last is not None and (now_m - last) < CONTROL_SESSION_REFRESH_MIN_INTERVAL:
                return True

            port = int(self.control_port)
            self._send(HELLO_PAYLOAD, "COMMAND_HELLO 1", port=port)
            await asyncio.sleep(0.1)
            if self._transport is not transport or transport.is_closing():
                return False
            self._send(HELLO_PAYLOAD, "COMMAND_HELLO 2", port=port)
            await asyncio.sleep(0.1)
            if self._transport is not transport or transport.is_closing():
                return False
            self._send(HELLO_PAYLOAD, "COMMAND_HELLO 3", port=port)
            await asyncio.sleep(0.1)
            if self._transport is not transport or transport.is_closing():
                return False
            self._send(INIT_SHORT, "COMMAND_INIT", port=port)
            self._last_control_session_refresh_monotonic = time.monotonic()
            return True

    async def async_require_control_session(self) -> None:
        """Raise a Home Assistant action error instead of silently dropping a command."""
        if not await self.async_prepare_control_session():
            raise HomeAssistantError("Tylo Sauna control session is unavailable")

    def is_online(self) -> bool:
        if self._transport is None or self._transport.is_closing():
            return False
        last = getattr(self, "last_rx_monotonic", None)
        if last is None:
            return False
        return (time.monotonic() - float(last)) <= ONLINE_TIMEOUT_S


    def start_watchdog(self) -> None:
        if self._unsub_watchdog is not None:
            return

        async def _tick(_now):
            online = self.is_online()
            now_m = time.monotonic()

            publish = False
            if online != self._online_cached:
                self._online_cached = online
                publish = True
                # Unpin telemetry host when going offline — allows re-learning on reconnect
                if not online and self.telemetry_host is not None:
                    _LOGGER.info(
                        "Tylo Sauna: unpinning telemetry_host=%s (offline)",
                        self.telemetry_host,
                    )
                    self.telemetry_host = None

            # once per minute publish a "heartbeat" so online/offline can update on its own
            if (now_m - self._last_publish_monotonic) >= 60:
                publish = True

            if publish:
                self._last_publish_monotonic = now_m
                self._notify_listeners()

            # If we're offline, periodically probe a set of candidate ports.
            # This helps when the controller changes its effective control port after reboot
            # and broadcasts are not visible to Home Assistant (common in some Docker setups).
            if not online:
                # Probe at most once per 30s to avoid spamming.
                if (now_m - float(self._last_offline_probe_monotonic)) >= 30:
                    self._last_offline_probe_monotonic = now_m
                    self._send_offline_probe()

        self._unsub_watchdog = async_track_time_interval(
            self._hass, _tick, timedelta(seconds=10)
        )

    def _send_offline_probe(self) -> None:
        """Send a lightweight handshake probe to a set of likely ports when offline.

        Important: some controllers appear to require a HELLO before INIT/telemetry starts,
        so we probe with HELLO + INIT_SHORT (very low frequency) to recover after reboot.
        """
        # Prefer the currently known ports first.
        ports = [
            int(getattr(self, "control_port", 0) or 0),
            int(getattr(self, "configured_port", 0) or 0),
            int(DEFAULT_CONTROL_PORT),
            *[int(p) for p in UDP_DISCOVERY_PORTS],
        ]
        # De-dup and remove invalid values.
        seen: set[int] = set()
        uniq: list[int] = []
        for p in ports:
            if p <= 0 or p > 65535:
                continue
            if p in seen:
                continue
            seen.add(p)
            uniq.append(p)

        # No transport yet -> nothing to do.
        if not self._transport:
            return

        for p in uniq:
            # Probe like the official app: HELLO then INIT.
            self._send(HELLO_PAYLOAD, desc=f"OFFLINE_PROBE_HELLO (port {p})", port=p)
            self._send(INIT_SHORT, desc=f"OFFLINE_PROBE_INIT (port {p})", port=p)

    def start_keepalive(self) -> None:
        if self._unsub_keepalive is not None:
            return

        async def _tick(_now):
            if not self._transport:
                return
            self._send(INIT_SHORT, "KEEPALIVE")

        self._unsub_keepalive = async_track_time_interval(
            self._hass, _tick, timedelta(seconds=KEEPALIVE_INTERVAL)
        )

    async def async_stop(self) -> None:
        self._stopping = True

        if self._reconnect_task:
            self._reconnect_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._reconnect_task
            self._reconnect_task = None

        if self._init_task:
            self._init_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._init_task
            self._init_task = None

        if self._unsub_keepalive:
            self._unsub_keepalive()
            self._unsub_keepalive = None

        if self._unsub_watchdog:
            self._unsub_watchdog()
            self._unsub_watchdog = None

        if self._transport:
            self._transport.close()
            self._transport = None



    def _send(self, payload: bytes, desc: str = "", port: int | None = None) -> None:
        transport = self._transport
        if transport is None or transport.is_closing():
            _LOGGER.warning("Tylo Sauna: transport not ready, cannot send %s", desc or "")
            if transport is not None:
                self.connection_lost(None, transport)
            return

        dst_port = int(port) if port is not None else int(self.control_port)
        transport.sendto(payload, (self.host, dst_port))
        self.tx_packets += 1
        self._debug_record("tx", (self.host, dst_port), payload, note=desc)
        if desc:
            _LOGGER.debug("Tylo Sauna: send %s (%d bytes) -> %s:%s", desc, len(payload), self.host, dst_port)

    def _debug_record(self, direction: str, addr: tuple, data: bytes, note: str = "") -> None:
        """Append a packet record to the debug ring buffer."""
        if not self.debug_recording:
            return
        self._debug_buffer.append({
            "ts": dt_util.utcnow().isoformat(),
            "dir": direction,
            "addr": f"{addr[0]}:{addr[1]}",
            "len": len(data),
            "hex": data.hex(),
            "note": note,
        })

    def _send_probe(self, payload: bytes, desc: str = "") -> None:
        """Send init packets to a small set of candidate ports when port is uncertain."""
        ports = sorted(self._probe_ports) if self._probe_ports else [self.control_port]
        for p in ports:
            suffix = f" (port {p})" if len(ports) > 1 else ""
            self._send(payload, desc + suffix, port=p)

    def connection_made(self, transport: asyncio.DatagramTransport) -> None:
        if self._stopping:
            transport.close()
            return
        self._transport = transport
        sockname = transport.get_extra_info("sockname")
        _LOGGER.info("Tylo Sauna: UDP socket bound on %s", sockname)

        # Start periodic jobs once transport exists
        self.start_keepalive()
        self.start_watchdog()


    def connection_lost(
        self,
        exc: Exception | None,
        transport: asyncio.DatagramTransport | None = None,
    ) -> None:
        _LOGGER.info("Tylo Sauna: connection lost: %s", exc)
        if transport is not None and transport is not self._transport:
            return
        if self._init_task:
            self._init_task.cancel()
            self._init_task = None
        self._transport = None
        self._protocol = None
        self._last_control_session_refresh_monotonic = None
        self._notify_listeners()
        self._schedule_reconnect()

    def datagram_received(self, data: bytes, addr) -> None:
        src_ip, src_port = addr

        # If GUID is not known (manual setup), try to learn it from incoming Tylo packets.
        # Not all firmwares include it in unicast telemetry, but when present it helps discovery-first caching.
        if getattr(self, "guid", None) is None:
            pkt_guid = _extract_guid_from_payload(data)
            if pkt_guid:
                try:
                    self.guid = pkt_guid
                    # Persist GUID into the config entry if available (migration without re-add).
                    entry_id = getattr(self, "entry_id", None)
                    domain_data = getattr(self._hass, "data", {}).get(DOMAIN, {})
                    entry = None
                    if entry_id and isinstance(domain_data, dict):
                        entry = domain_data.get(str(entry_id), {}).get("entry")
                    if entry and not entry.data.get("guid"):
                        new_data = {**entry.data, "guid": str(pkt_guid)}
                        self._hass.config_entries.async_update_entry(entry, data=new_data)
                    # Persist endpoint cache if store exists.
                    store = domain_data.get("_endpoint_store") if isinstance(domain_data, dict) else None
                    if store:
                        try:
                            self._hass.async_create_task(
                                store.set(
                                    guid=str(pkt_guid),
                                    # Кэшируем endpoint из фактического источника пакета, а не из текущего self.host
                                    # (на Docker/macOS self.host может быть неверно переписан в 127.0.0.1).
                                    host=str(src_ip),
                                    port=int(src_port),
                                    source="telemetry",
                                )
                            )
                        except Exception:  # noqa: BLE001
                            pass
                    _LOGGER.info("Tylo Sauna: learned guid=%s from telemetry", pkt_guid)
                except Exception:  # noqa: BLE001
                    pass


        # Smart telemetry filtering: accept from pinned host, or auto-learn from valid Tylo packets.
        if self.telemetry_host is not None:
            # Pinned — only accept from pinned host
            if src_ip != self.telemetry_host:
                _LOGGER.debug(
                    "Tylo Sauna: ignoring telemetry from %s (pinned telemetry_host=%s)",
                    src_ip, self.telemetry_host
                )
                self._debug_record("rx_filtered", addr, data, note=f"expected pinned {self.telemetry_host}")
                return
        else:
            # Not pinned — accept from configured host, or auto-learn from valid Tylo packets
            if src_ip == self.host:
                pass  # accept from configured host
            else:
                if not _looks_like_tylo_telemetry(data):
                    _LOGGER.debug(
                        "Tylo Sauna: ignoring non-telemetry UDP packet from %s", src_ip
                    )
                    self._debug_record("rx_filtered", addr, data, note="not telemetry-like")
                    return

                pkt_guid = _extract_guid_from_payload(data)
                if self.guid and pkt_guid and pkt_guid != self.guid:
                    _LOGGER.warning(
                        "Tylo Sauna: telemetry GUID mismatch from %s: packet_guid=%s, entry_guid=%s. Ignoring.",
                        src_ip, pkt_guid, self.guid
                    )
                    self._debug_record("rx_filtered", addr, data, note=f"guid mismatch: pkt={pkt_guid} entry={self.guid}")
                    return

                # Accept & pin
                self.telemetry_host = src_ip
                _LOGGER.warning(
                    "Tylo Sauna: telemetry received from %s (configured host=%s). "
                    "Pinning telemetry_host=%s (guid_hint=%s).",
                    src_ip, self.host, src_ip, pkt_guid or "n/a"
                )

        # Learn actual control port from incoming Tylo packets (helps Docker users who kept discovery port).
        self._maybe_learn_control_port(src_port, data)

        self.last_rx_ip = src_ip
        self.last_rx_port = int(src_port)

        self.rx_packets += 1
        self.last_rx_monotonic = time.monotonic()
        self.last_rx_dt = dt_util.utcnow()
        self._debug_record("rx", addr, data)

        # --- Fault events (door cancel etc.) ---
        if data.startswith(b"\xf2\x83\x01") or b"\xf2\x83\x01" in data:
            fault = parse_fault_event(data)
            if fault is not None:
                prev_pending = self.door_fault_pending
                prev_fault = self.last_fault
                self.last_fault = fault
                self.door_fault_pending = (
                    fault.code in DOOR_FAULT_CODES and fault.state == STATE_PENDING_ACK
                )
                if prev_pending != self.door_fault_pending or prev_fault != self.last_fault:
                    _LOGGER.info(
                        "Tylo Sauna fault: code=%s state=%s pending_ack=%s msg=%s",
                        fault.code,
                        fault.state,
                        self.door_fault_pending,
                        fault.message,
                    )
                    self._notify_listeners()
                return

        # --- Favorites snapshot ---
        if data.startswith(b"\xc2\x7f"):
            favs = parse_favorites_snapshot(data)
            if favs and favs != self.favorites:
                self.favorites = favs
                _LOGGER.info("Tylo Sauna: favorites updated (%d slots)", len(favs))
                self._notify_listeners()
            return

        # --- Mode response (9280010258XX) ---
        # Response after mode change command: 0a=OFF, 0b=HEAT, 0c=STANDBY
        mode_pattern = bytes.fromhex("9280010258")
        if mode_pattern in data:
            idx = data.find(mode_pattern)
            if idx + len(mode_pattern) < len(data):
                mode_byte = data[idx + len(mode_pattern)]
                if mode_byte in (MODE_OFF, MODE_HEAT, MODE_STANDBY):
                    if mode_byte != self.current_mode:
                        self.current_mode = mode_byte
                        mode_names = {MODE_OFF: "OFF", MODE_HEAT: "HEAT", MODE_STANDBY: "STANDBY"}
                        _LOGGER.info("Tylo Sauna mode: %s (0x%02x)", mode_names.get(mode_byte, "?"), mode_byte)
                        self._notify_listeners()

        # --- Aroma events (steam controllers) ---
        # Стабильный признак: начинается с `b2 81 01` (field 2070, length-delimited).
        if self.experimental_aroma and data.startswith(AROMA_EVENT_PREFIX):
            try:
                name, flag = self._parse_aroma_event(data)
                if name is not None:
                    self.last_aroma_name = str(name)
                if flag is not None:
                    self.last_aroma_flag = int(flag)
                self.last_aroma_dt = dt_util.utcnow()
                _LOGGER.info(
                    "Tylo Steam aroma event: name=%s flag=%s", self.last_aroma_name, self.last_aroma_flag
                )
                self._notify_listeners()
            except Exception:  # noqa: BLE001
                pass
            # Не return: после события иногда идут телеметрийные куски, но этот пакет нам не нужен как telemetry.
            return

        self._handle_telemetry(data)

    def _maybe_learn_control_port(self, src_port: int, data: bytes) -> None:
        # Only learn from packets that look like Tylo telemetry/config/events.
        if not _looks_like_tylo_telemetry(data):
            return

        # If we know the GUID, require it to match before accepting a port update.
        pkt_guid = _extract_guid_from_payload(data)
        if self.guid and pkt_guid and pkt_guid != self.guid:
            return

        src_port = int(src_port)
        self.maybe_update_control_port(src_port, source="telemetry", src_ip=str(getattr(self, "last_rx_ip", None) or self.host), guid=getattr(self, "guid", None))

    # === Telemetry parsing ===

    def _handle_telemetry(self, data: bytes) -> None:
        changed = False

        def _parse_light_flag_from_bytes(buf: bytes) -> bool | None:
            """Parse the light flag from any buffer by searching known da7d light patterns."""
            patterns = (
                bytes.fromhex("da7d04080a10"),  # common
                bytes.fromhex("da7d05080a10"),  # observed variant (length differs)
            )
            best_idx = -1
            best_pat = None
            for pat in patterns:
                idx = buf.rfind(pat)
                if idx > best_idx:
                    best_idx = idx
                    best_pat = pat
            if best_idx != -1 and best_pat is not None and best_idx + len(best_pat) < len(buf):
                b = buf[best_idx + len(best_pat)]
                if b in (0, 1):
                    return bool(b)
            return None

        # Flags (e.g., light, standby_enabled)
        if data.startswith(b"\xda\x7d"):
            # Prefer the legacy pattern-based parsing for stability.
            # Observed flag form: da 7d 04 08 0a 10 <0|1>
            val = _parse_light_flag_from_bytes(data)
            if val is not None and val != self.light:
                self.light = val
                changed = True

            # Standby enabled flag (field 0x0b): da 7d 04 08 0b 10 <0|1>
            standby_patterns = (
                bytes.fromhex("da7d04080b10"),
                bytes.fromhex("da7d05080b10"),
            )
            for pat in standby_patterns:
                idx = data.find(pat)
                if idx != -1 and idx + len(pat) < len(data):
                    sb_val = bool(data[idx + len(pat)])
                    if sb_val != self.standby_enabled:
                        self.standby_enabled = sb_val
                        _LOGGER.info("Tylo Sauna standby_enabled: %s", sb_val)
                        changed = True
                    break

            # Schedule / programs (field 2045 sub-messages inside da7d packets)
            try:
                sched = parse_schedule_slots(data)
                if sched != self.schedule:
                    self.schedule = sched
                    _LOGGER.info("Tylo Sauna: schedule updated (%d programs)", len(sched))
                    changed = True
            except Exception:  # noqa: BLE001
                pass

        # Status KV (temps, timers, etc.)
        if data.startswith(b"\xd2\x7d"):
            # Prefer legacy prefix parsing for known fields (stable across current sauna firmwares).
            # This avoids accidental KV collisions in nested messages.
            vals: dict[str, Any] = {}

            # stop_cfg_min
            stop_cfg = None
            for prefix_hex in ("d27d05081110", "d27d04081110"):
                stop_cfg = _parse_varint_after(data, prefix_hex)
                if stop_cfg is not None:
                    break
            if stop_cfg is not None:
                vals["stop_cfg_min"] = int(stop_cfg)

            # stop_rem_min
            stop_rem = None
            for prefix_hex in ("d27d05081610", "d27d04081610"):
                stop_rem = _parse_varint_after(data, prefix_hex)
                if stop_rem is not None:
                    break
            if stop_rem is not None:
                vals["stop_rem_min"] = int(stop_rem)

            # temperatures (raw = °C * 9)
            t_set_raw = _parse_varint_after(data, "d27d05080a10")
            if t_set_raw is not None:
                vals["t_set_c"] = float(t_set_raw) / TEMP_SCALE

            t_cur_raw = _parse_varint_after(data, "d27d05080c10")
            if t_cur_raw is not None:
                vals["t_cur_c"] = float(t_cur_raw) / TEMP_SCALE

            # standby temperature delta (field 0x0b): d27d 05 08 0b 10 <varint>
            standby_delta_raw = _parse_varint_after(data, "d27d05080b10")
            if standby_delta_raw is not None:
                vals["standby_delta_c"] = float(standby_delta_raw) / TEMP_SCALE

            # current humidity % (field 0x13): Combi/Steam setups
            humidity_cur = None
            for prefix_hex in ("d27d04081310", "d27d05081310"):
                humidity_cur = _parse_varint_after(data, prefix_hex)
                if humidity_cur is not None:
                    break
            if humidity_cur is not None:
                vals["humidity_cur_pct"] = int(humidity_cur)

            # humidity setpoint % (field 0x14): Combi/Steam setups
            humidity_set = None
            for prefix_hex in ("d27d04081410", "d27d05081410"):
                humidity_set = _parse_varint_after(data, prefix_hex)
                if humidity_set is not None:
                    break
            if humidity_set is not None:
                vals["humidity_set_pct"] = int(humidity_set)

            # Light flag is sometimes embedded inside the status packet (observed in captures).
            # Prefer this over any unrelated da7d packets that may contain other internal flags.
            light_val = _parse_light_flag_from_bytes(data)
            if light_val is not None and light_val != self.light:
                self.light = light_val
                changed = True

            # Fallback: generic KV extractor to help future variants (e.g., steam/humidity)
            # but only fill missing values (never overwrite stable legacy parsed values).
            if len(vals) < 4:
                fallback = _extract_mapped_fields(data, STATUS_KV_MAP)
                for k, v in fallback.items():
                    vals.setdefault(k, v)

            if "stop_cfg_min" in vals and vals["stop_cfg_min"] != self.stop_cfg_min:
                self.stop_cfg_min = int(vals["stop_cfg_min"])
                changed = True

            if "stop_rem_min" in vals and vals["stop_rem_min"] != self.stop_rem_min:
                self.stop_rem_min = int(vals["stop_rem_min"])
                changed = True

            if "t_set_c" in vals and vals["t_set_c"] != self.t_set_c:
                self.t_set_c = float(vals["t_set_c"])
                changed = True

            if "t_cur_c" in vals and vals["t_cur_c"] != self.t_cur_c:
                self.t_cur_c = float(vals["t_cur_c"])
                changed = True

            if "standby_delta_c" in vals and vals["standby_delta_c"] != self.standby_delta_c:
                self.standby_delta_c = float(vals["standby_delta_c"])
                _LOGGER.info("Tylo Sauna standby_delta: %.1f°C", self.standby_delta_c)
                changed = True

            if "humidity_set_pct" in vals and vals["humidity_set_pct"] != self.humidity_set_pct:
                self.humidity_set_pct = int(vals["humidity_set_pct"])
                changed = True

            if "humidity_cur_pct" in vals and vals["humidity_cur_pct"] != self.humidity_cur_pct:
                self.humidity_cur_pct = int(vals["humidity_cur_pct"])
                changed = True

        # Derive HEAT from remaining time if available.
        new_heat = None
        if self.stop_rem_min is not None:
            new_heat = self.stop_rem_min > 0
        if new_heat is not None and new_heat != self.heat:
            self.heat = new_heat
            changed = True

        if changed:
            telemetry_src = self.telemetry_host or self.host
            _LOGGER.info(
                "Tylo Sauna state: LIGHT=%s, HEAT=%s, Tset=%s°C, Tcur=%s°C, StopCfg=%s, StopRem=%s, "
                "Hum=%s%% "
                "(telemetry_host=%s, rx=%d, tx=%d)",
                self.light,
                self.heat,
                f"{self.t_set_c:.1f}" if self.t_set_c is not None else "?",
                f"{self.t_cur_c:.1f}" if self.t_cur_c is not None else "?",
                self.stop_cfg_min if self.stop_cfg_min is not None else "?",
                self.stop_rem_min if self.stop_rem_min is not None else "?",
                self.humidity_cur_pct if self.humidity_cur_pct is not None else "?",
                telemetry_src,
                self.rx_packets,
                self.tx_packets,
            )
            self._notify_listeners()

    # === API for entities ===

    def register_callback(self, cb) -> None:
        self._callbacks.append(cb)

    def unregister_callback(self, cb) -> None:
        try:
            self._callbacks.remove(cb)
        except ValueError:
            pass

    def _notify_listeners(self) -> None:
        for cb in list(self._callbacks):
            try:
                cb()
            except Exception as exc:  # noqa: BLE001
                _LOGGER.exception("Tylo Sauna callback error: %s", exc)

    # --- Commands ---

    def request_favorites(self) -> None:
        """Request favorites list from the controller."""
        self._send(FAVORITES_REQ, "REQ_FAVORITES")

    def light_on(self) -> None:
        self._send(LIGHT_ON_PAYLOAD, "LIGHT ON")

    def light_off(self) -> None:
        self._send(LIGHT_OFF_PAYLOAD, "LIGHT OFF")

    def heat_on(self) -> None:
        self._send(HEAT_ON_PAYLOAD, "HEAT ON")
        self._send(HEAT_AUX_PAYLOAD, "HEAT AUX")

    def heat_off(self) -> None:
        self._send(HEAT_OFF_PAYLOAD, "HEAT OFF")
        self._send(HEAT_AUX_PAYLOAD, "HEAT AUX")

    def standby(self) -> None:
        """Activate standby mode (reduced temperature heating)."""
        self._send(STANDBY_PAYLOAD, "STANDBY")
        self._send(HEAT_AUX_PAYLOAD, "STANDBY AUX")

    async def async_set_temperature(self, temp_c: float) -> None:
        await self.async_require_control_session()
        raw = int(round(temp_c * 9.0))
        prefix = bytes.fromhex("d24105080a10")
        payload = prefix + _encode_varint(raw)
        self._send(payload, f"SETTEMP {temp_c:.1f}°C")

    async def async_set_stop_after(self, minutes: int) -> None:
        await self.async_require_control_session()
        m = int(minutes)
        var = _encode_varint(m)
        p1 = bytes.fromhex("d24105080e10") + var
        p2 = bytes.fromhex("d23e020801")
        self._send(p1, f"SETSTOP {m} min (cfg)")
        await asyncio.sleep(0.02)
        self._send(p2, "SETSTOP aux")

    async def async_apply_favorite(self, slot: int, start: bool = False) -> None:
        """Apply a favorite preset as a 'scene' (temp + stop-after + light), optionally start."""
        fav = self.favorites.get(int(slot))
        if not fav or not fav.enabled:
            _LOGGER.warning("Tylo Sauna: favorite slot %s not available", slot)
            return
        await self.async_require_control_session()

        # Light
        if fav.light_on is not None:
            if fav.light_on:
                self.light_on()
            else:
                self.light_off()

        # Temperature
        if fav.target_temp_c is not None:
            await self.async_set_temperature(float(fav.target_temp_c))

        # Stop-after
        if fav.stop_after_min is not None:
            await self.async_set_stop_after(int(fav.stop_after_min))

        self.last_selected_favorite_slot = int(slot)
        self._notify_listeners()

        if start:
            self.heat_on()

    async def async_ack_last_fault(self) -> None:
        """Acknowledge the last fault popup (e.g. door cancel) like the official app."""
        if not self.last_fault:
            _LOGGER.warning("Tylo Sauna: no last_fault to acknowledge")
            return
        await self.async_require_control_session()
        payload = encode_fault_ack(self.last_fault)
        self._send(payload, "ACK_FAULT")

    # --- Steam/Aroma commands (экспериментально) ---

    def aroma_eucalyptus_on(self) -> None:
        """Включить ароматизацию (Eucalyptus) — экспериментально."""
        if not getattr(self, "experimental_aroma", False):
            _LOGGER.warning("Tylo Sauna: aroma command ignored (experimental_aroma disabled)")
            return
        self._send(AROMA_EUCALYPTUS_ON, "AROMA_EUCALYPTUS_ON")

    def aroma_eucalyptus_off(self) -> None:
        """Выключить ароматизацию (Eucalyptus) — экспериментально."""
        if not getattr(self, "experimental_aroma", False):
            _LOGGER.warning("Tylo Sauna: aroma command ignored (experimental_aroma disabled)")
            return
        self._send(AROMA_EUCALYPTUS_OFF, "AROMA_EUCALYPTUS_OFF")

    def _parse_aroma_event(self, payload: bytes) -> tuple[str | None, int | None]:
        """Best-effort парсер echo от steam-контроллера после aroma-команды.

        Ожидаем структуру:
        - field 2070 (len-delimited) → внутри field2 (len-delimited, тот же body что у `92 44 ...`)
        - у body: field3 = bytes (как repeated varint), field4 = 0/1 (varint)
        """
        top = _pb_collect(payload)
        # field 2070
        items = top.get(2070, [])
        if not items:
            return None, None

        wt, raw = items[0]
        if wt != 2:
            return None, None

        inner = _pb_collect(raw)
        # В observed payload у echo есть field2: bytes (len=0x1a)
        body_raw = None
        for wti, vi in inner.get(2, []):
            if wti == 2 and isinstance(vi, (bytes, bytearray)):
                body_raw = bytes(vi)
                break
        if not body_raw:
            return None, None

        body = _pb_collect(body_raw)
        name = None
        flag = _pb_first_varint(body, 4)
        try:
            # field3 как "repeated bytes via varint"
            name = bytes(_pb_all_varints(body, 3)).decode("utf-8", errors="replace")
            if name:
                name = name.strip()
        except Exception:  # noqa: BLE001
            name = None

        return name, int(flag) if flag is not None else None
