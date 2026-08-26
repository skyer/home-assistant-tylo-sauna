from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any, cast

from custom_components.tylo_sauna.climate import TyloSaunaClimate
from custom_components.tylo_sauna.controller import (
    HELLO_PAYLOAD,
    INIT_SHORT,
    SaunaController,
    SaunaProtocol,
)
from custom_components.tylo_sauna import controller as controller_module


class FakeHass:
    def __init__(self) -> None:
        self.loop = asyncio.get_running_loop()
        self.tracked_intervals: list = []

    def create_task(self, coroutine):
        return asyncio.create_task(coroutine)

    def async_create_task(self, coroutine):
        return asyncio.create_task(coroutine)


class FakeTransport:
    def __init__(self) -> None:
        self.sent: list[tuple[bytes, tuple[str, int]]] = []
        self.closed = False

    def sendto(self, payload: bytes, destination: tuple[str, int]) -> None:
        self.sent.append((payload, destination))

    def close(self) -> None:
        self.closed = True

    def is_closing(self) -> bool:
        return self.closed

    def get_extra_info(self, name: str):
        if name == "sockname":
            return ("0.0.0.0", 12345)
        return None


def make_controller() -> SaunaController:
    return SaunaController(FakeHass(), "Sauna", "192.0.2.10", port=42156)


def test_prepare_control_session_refreshes_once_for_command_burst() -> None:
    async def scenario() -> None:
        controller = make_controller()
        transport = FakeTransport()
        controller._transport = cast(Any, transport)

        assert await asyncio.gather(
            controller.async_prepare_control_session(),
            controller.async_prepare_control_session(),
        ) == [True, True]
        assert [payload for payload, _destination in transport.sent] == [
            HELLO_PAYLOAD,
            HELLO_PAYLOAD,
            HELLO_PAYLOAD,
            INIT_SHORT,
        ]

        assert await controller.async_prepare_control_session()
        assert len(transport.sent) == 4

    asyncio.run(scenario())


def test_temperature_command_refreshes_control_session_before_actuation() -> None:
    async def scenario() -> None:
        controller = make_controller()
        transport = FakeTransport()
        controller._transport = cast(Any, transport)

        await controller.async_set_temperature(80.0)

        assert [payload for payload, _destination in transport.sent[:4]] == [
            HELLO_PAYLOAD,
            HELLO_PAYLOAD,
            HELLO_PAYLOAD,
            INIT_SHORT,
        ]
        assert len(transport.sent) == 5
        assert transport.sent[-1][0] not in (HELLO_PAYLOAD, INIT_SHORT)

    asyncio.run(scenario())


def test_connection_loss_clears_transport_and_schedules_one_reconnect(monkeypatch) -> None:
    async def scenario() -> None:
        controller = make_controller()
        transport = FakeTransport()
        controller._transport = cast(Any, transport)
        reconnect_started = asyncio.Event()
        reconnect_release = asyncio.Event()

        async def fake_reconnect() -> None:
            reconnect_started.set()
            await reconnect_release.wait()

        monkeypatch.setattr(controller, "_async_reconnect", fake_reconnect)

        controller.connection_lost(RuntimeError("socket closed"), cast(Any, transport))
        await reconnect_started.wait()
        first_task = controller._reconnect_task

        controller.connection_lost(RuntimeError("duplicate callback"), cast(Any, transport))
        assert controller._transport is None
        assert controller._reconnect_task is first_task

        reconnect_release.set()
        await first_task

    asyncio.run(scenario())


def test_reconnect_recreates_transport_and_replays_initial_handshake() -> None:
    async def scenario() -> None:
        controller = make_controller()
        transports: list[FakeTransport] = []

        class FakeLoop:
            async def create_datagram_endpoint(self, factory, *, local_addr):
                assert local_addr == ("0.0.0.0", 0)
                transport = FakeTransport()
                protocol = factory()
                protocol.connection_made(transport)
                transports.append(transport)
                return transport, protocol

        controller._hass.loop = FakeLoop()
        await controller._async_reconnect()
        assert controller._transport is transports[0]
        assert controller._init_task is not None
        await controller._init_task
        assert [payload for payload, _destination in transports[0].sent[:4]] == [
            HELLO_PAYLOAD,
            HELLO_PAYLOAD,
            HELLO_PAYLOAD,
            INIT_SHORT,
        ]

    asyncio.run(scenario())


def test_protocol_connection_lost_wires_transport_to_single_reconnect(monkeypatch) -> None:
    async def scenario() -> None:
        controller = make_controller()
        protocol = SaunaProtocol(controller)
        transport = FakeTransport()
        reconnect_started = asyncio.Event()
        reconnect_release = asyncio.Event()

        async def fake_reconnect() -> None:
            reconnect_started.set()
            await reconnect_release.wait()

        monkeypatch.setattr(controller, "_async_reconnect", fake_reconnect)
        protocol.connection_made(cast(Any, transport))
        assert protocol.transport is transport
        assert controller._transport is transport

        protocol.connection_lost(RuntimeError("kernel socket failure"))
        await reconnect_started.wait()
        reconnect_task = controller._reconnect_task
        assert reconnect_task is not None
        assert controller._transport is None

        protocol.connection_lost(RuntimeError("duplicate callback"))
        assert controller._reconnect_task is reconnect_task

        reconnect_release.set()
        await reconnect_task

    asyncio.run(scenario())


def test_stop_cancels_reconnect_and_prevents_endpoint_resurrection() -> None:
    async def scenario() -> None:
        controller = make_controller()
        transport = FakeTransport()
        controller._transport = cast(Any, transport)

        async def wait_forever() -> None:
            await asyncio.Event().wait()

        controller._reconnect_task = asyncio.create_task(wait_forever())
        controller._init_task = asyncio.create_task(wait_forever())
        await controller.async_stop()

        assert controller._stopping is True
        assert controller._transport is None
        assert controller._reconnect_task is None
        assert controller._init_task is None
        assert transport.closed is True

    asyncio.run(scenario())


def test_closing_transport_is_rejected_before_command_refresh() -> None:
    async def scenario() -> None:
        controller = make_controller()
        transport = FakeTransport()
        transport.closed = True
        controller._transport = cast(Any, transport)
        controller._stopping = True

        assert await controller.async_prepare_control_session() is False
        assert transport.sent == []

    asyncio.run(scenario())


def test_stop_during_endpoint_open_does_not_retain_closed_transport() -> None:
    async def scenario() -> None:
        controller = make_controller()
        transport = FakeTransport()

        class StoppingLoop:
            async def create_datagram_endpoint(self, factory, *, local_addr):
                protocol = factory()
                protocol.connection_made(transport)
                controller._stopping = True
                return transport, protocol

        controller._hass.loop = StoppingLoop()
        await controller._async_open_transport()

        assert transport.closed is True
        assert controller._transport is None
        assert controller._protocol is None

    asyncio.run(scenario())


def test_actuation_surfaces_unavailable_control_session() -> None:
    async def scenario() -> None:
        controller = make_controller()

        try:
            await controller.async_set_temperature(80.0)
        except RuntimeError as exc:
            assert str(exc) == "Tylo Sauna control session is unavailable"
        else:
            raise AssertionError("actuation silently succeeded without a control session")

    asyncio.run(scenario())


def test_transport_loss_mid_refresh_aborts_before_command_init() -> None:
    async def scenario() -> None:
        controller = make_controller()

        class ClosingTransport(FakeTransport):
            def sendto(self, payload: bytes, destination: tuple[str, int]) -> None:
                super().sendto(payload, destination)
                self.closed = True

        transport = ClosingTransport()
        controller._transport = cast(Any, transport)
        controller._stopping = True

        assert await controller.async_prepare_control_session() is False
        assert [payload for payload, _destination in transport.sent] == [HELLO_PAYLOAD]

    asyncio.run(scenario())


def test_closed_transport_send_path_schedules_one_reconnect(monkeypatch) -> None:
    async def scenario() -> None:
        controller = make_controller()
        transport = FakeTransport()
        transport.closed = True
        controller._transport = cast(Any, transport)
        reconnect_started = asyncio.Event()
        reconnect_release = asyncio.Event()

        async def fake_reconnect() -> None:
            reconnect_started.set()
            await reconnect_release.wait()

        monkeypatch.setattr(controller, "_async_reconnect", fake_reconnect)
        controller._send(INIT_SHORT, "test")
        await reconnect_started.wait()
        reconnect_task = controller._reconnect_task
        assert reconnect_task is not None
        controller._send(INIT_SHORT, "duplicate")

        assert controller._reconnect_task is reconnect_task
        reconnect_release.set()
        await reconnect_task

    asyncio.run(scenario())


def test_climate_uses_udp_liveness_for_availability_and_heartbeat() -> None:
    async def scenario() -> None:
        controller = make_controller()
        controller.last_rx_monotonic = 1.0
        controller.is_online = lambda: True
        controller.last_rx_dt = datetime(2026, 8, 25, 18, 29, 59, tzinfo=UTC)
        entity = TyloSaunaClimate(controller)

        assert entity.available is True
        assert entity.extra_state_attributes["connection_last_seen"] == datetime(
            2026, 8, 25, 18, 29, tzinfo=UTC
        )

        controller.last_rx_dt = datetime(2026, 8, 25, 18, 29, 1, tzinfo=UTC)
        assert entity.extra_state_attributes["connection_last_seen"] == datetime(
            2026, 8, 25, 18, 29, tzinfo=UTC
        )

        controller.last_rx_dt = datetime(2026, 8, 25, 18, 30, 1, tzinfo=UTC)
        assert entity.extra_state_attributes["connection_last_seen"] == datetime(
            2026, 8, 25, 18, 30, tzinfo=UTC
        )

    asyncio.run(scenario())


def test_idle_live_rx_advances_climate_state_once_per_minute(monkeypatch) -> None:
    async def scenario() -> None:
        clock = [0.0]
        monkeypatch.setattr(controller_module.time, "monotonic", lambda: clock[0])
        controller = make_controller()
        transport = FakeTransport()
        controller._transport = cast(Any, transport)
        controller.last_rx_monotonic = 0.0
        controller.last_rx_dt = datetime(2026, 8, 25, 18, 29, 59, tzinfo=UTC)
        controller.current_mode = 10
        entity = TyloSaunaClimate(controller)
        written_snapshots: list[tuple] = []

        def write_state() -> None:
            snapshot = (
                entity.available,
                entity.hvac_mode,
                entity.current_temperature,
                entity.target_temperature,
                tuple(sorted(entity.extra_state_attributes.items())),
            )
            if not written_snapshots or snapshot != written_snapshots[-1]:
                written_snapshots.append(snapshot)

        controller.register_callback(write_state)
        write_state()
        controller.start_watchdog()
        watchdog_tick = controller._hass.tracked_intervals[0]

        clock[0] = 61.0
        controller.last_rx_monotonic = clock[0]
        controller.last_rx_dt = datetime(2026, 8, 25, 18, 30, 1, tzinfo=UTC)
        await watchdog_tick(None)
        assert len(written_snapshots) == 2

        controller.last_rx_dt = datetime(2026, 8, 25, 18, 30, 45, tzinfo=UTC)
        controller._notify_listeners()
        assert len(written_snapshots) == 2

        clock[0] = 122.0
        controller.last_rx_monotonic = clock[0]
        controller.last_rx_dt = datetime(2026, 8, 25, 18, 31, 1, tzinfo=UTC)
        await watchdog_tick(None)
        assert len(written_snapshots) == 3

        clock[0] = 183.0
        await watchdog_tick(None)
        assert len(written_snapshots) == 3

        clock[0] = 500.0
        await watchdog_tick(None)
        assert entity.available is False
        assert len(written_snapshots) == 4

        clock[0] = 561.0
        await watchdog_tick(None)
        assert len(written_snapshots) == 4

    asyncio.run(scenario())
