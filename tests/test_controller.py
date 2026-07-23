from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace

from mwbc.config import AppConfig, PeerConfig
from mwbc.controller import ActiveRemote, BorderController
from mwbc.geometry import Point
from mwbc.input_backend import NullBackend
from mwbc.state import StateStore


class TrackingBackend(NullBackend):
    def __init__(self) -> None:
        super().__init__()
        self.capture_modes: list[bool] = []

    def start_capture(self, callbacks, *, suppress: bool) -> None:
        self.capture_modes.append(suppress)


class FakeRemoteClient:
    def __init__(self, *, connected: bool = True) -> None:
        self._connected = connected
        self.remote_screen = SimpleNamespace(width=1680, height=1050)
        self.sent: list[tuple[str, dict]] = []

    @property
    def connected(self) -> bool:
        return self._connected

    async def connect(self) -> None:
        if not self._connected:
            raise ConnectionError("fake client disconnected")

    async def send(self, message_type: str, payload: dict) -> None:
        if not self._connected:
            raise ConnectionError("fake client disconnected")
        self.sent.append((message_type, payload))


class ControllerTests(unittest.IsolatedAsyncioTestCase):
    async def test_refresh_peers_updates_edges_and_direct_clients(self) -> None:
        config = AppConfig(machine_name="host", peers=[PeerConfig(name="mac", edge="left")])
        backend = NullBackend()
        state = StateStore("host", backend.name)
        controller = BorderController(config, backend, state)

        self.assertNotIn("mac", controller.clients)
        config.peers[0].host = "127.0.0.1"
        config.peers[0].edge = "top"

        await controller.refresh_peers()

        self.assertIn("mac", controller.clients)
        self.assertEqual(state.snapshot()["peers"][0]["edge"], "top")

        config.peers[0].host = ""
        await controller.refresh_peers()

        self.assertNotIn("mac", controller.clients)

    async def test_recover_local_restores_host_after_remote_failure(self) -> None:
        peer = PeerConfig(name="mac", edge="left")
        config = AppConfig(machine_name="host", peers=[peer])
        backend = TrackingBackend()
        state = StateStore("host", backend.name)
        controller = BorderController(config, backend, state)
        controller.active = ActiveRemote(peer=peer, client=FakeRemoteClient(), point=SimpleNamespace(x=5, y=5))
        state.update(active_peer="mac")
        controller._start_capture(suppress=True)

        await controller._recover_local("mac disconnected")

        snapshot = state.snapshot()
        self.assertIsNone(controller.active)
        self.assertIsNone(snapshot["active_peer"])
        self.assertEqual(backend.current_position(), (2, 5))
        self.assertEqual(backend.capture_modes[-2:], [True, False])
        self.assertEqual(snapshot["peers"][0]["error"], "mac disconnected")

    async def test_active_watch_recovers_when_active_client_disconnects(self) -> None:
        peer = PeerConfig(name="mac", edge="left")
        config = AppConfig(machine_name="host", peers=[peer])
        backend = TrackingBackend()
        state = StateStore("host", backend.name)
        controller = BorderController(config, backend, state)
        await controller.start()
        controller.active = ActiveRemote(
            peer=peer,
            client=FakeRemoteClient(connected=False),
            point=SimpleNamespace(x=5, y=5),
        )
        state.update(active_peer="mac")
        controller._start_capture(suppress=True)

        try:
            for _ in range(20):
                if controller.active is None:
                    break
                await asyncio.sleep(0.05)
            self.assertIsNone(controller.active)
            self.assertIsNone(state.snapshot()["active_peer"])
            self.assertFalse(backend.capture_modes[-1])
        finally:
            await controller.stop()

    async def test_host_lock_hotkey_pauses_edge_switching(self) -> None:
        peer = PeerConfig(name="mac", edge="left")
        config = AppConfig(machine_name="host", peers=[peer])
        backend = TrackingBackend()
        state = StateStore("host", backend.name)
        controller = BorderController(config, backend, state)

        handled = await controller._handle_host_lock_hotkey({"kind": "special", "value": "f12"}, pressed=True)
        await controller._maybe_activate(Point(0, 500))

        self.assertTrue(handled)
        self.assertTrue(controller.edge_switching_paused)
        self.assertIsNone(controller.active)
        self.assertTrue(state.snapshot()["edge_switching_paused"])

    async def test_host_lock_hotkey_returns_from_active_remote_without_disconnect(self) -> None:
        peer = PeerConfig(name="mac", edge="left")
        config = AppConfig(machine_name="host", peers=[peer])
        backend = TrackingBackend()
        state = StateStore("host", backend.name)
        controller = BorderController(config, backend, state)
        client = FakeRemoteClient(connected=True)
        controller.active = ActiveRemote(peer=peer, client=client, point=SimpleNamespace(x=5, y=5))
        state.update(active_peer="mac")
        state.update_peer("mac", connected=True, error=None)
        controller._start_capture(suppress=True)

        await controller._handle_host_lock_hotkey({"kind": "special", "value": "f12"}, pressed=True)

        peer_state = state.snapshot()["peers"][0]
        self.assertTrue(controller.edge_switching_paused)
        self.assertIsNone(controller.active)
        self.assertIsNone(state.snapshot()["active_peer"])
        self.assertTrue(peer_state["connected"])
        self.assertIsNone(peer_state["error"])
        self.assertEqual(backend.capture_modes[-2:], [True, False])

    async def test_left_edge_activation_ignores_stale_pre_lock_motion(self) -> None:
        peer = PeerConfig(name="mac", edge="left")
        config = AppConfig(machine_name="host", peers=[peer])
        backend = TrackingBackend()
        state = StateStore("host", backend.name)
        controller = BorderController(config, backend, state)
        client = FakeRemoteClient(connected=True)

        async def resolve_client(_peer):
            return client

        controller._resolve_client = resolve_client  # type: ignore[method-assign]

        await controller._maybe_activate(Point(0, 500))

        self.assertIsNotNone(controller.active)
        self.assertEqual(
            client.sent,
            [
                ("control", {"active": True}),
                ("input", {"action": "move", "x": 1678, "y": 486}),
            ],
        )

        await controller._handle_move(0, 500)
        self.assertEqual(len(client.sent), 2)

        await controller._handle_move(controller.lock_point.x, controller.lock_point.y)
        await controller._handle_move(controller.lock_point.x - 5, controller.lock_point.y)

        self.assertEqual(client.sent[-1], ("input", {"action": "move", "x": 1673, "y": 486}))

    async def test_left_edge_activation_drops_stale_motion_after_grace_window(self) -> None:
        peer = PeerConfig(name="mac", edge="left")
        config = AppConfig(machine_name="host", peers=[peer])
        backend = TrackingBackend()
        state = StateStore("host", backend.name)
        controller = BorderController(config, backend, state)
        client = FakeRemoteClient(connected=True)

        async def resolve_client(_peer):
            return client

        controller._resolve_client = resolve_client  # type: ignore[method-assign]

        await controller._maybe_activate(Point(0, 500))
        controller._ignore_lock_motion_until = 0.0
        await controller._handle_move(0, 500)

        self.assertEqual(len(client.sent), 2)
        self.assertEqual(controller.active.point, Point(1678, 486))
        self.assertEqual(backend.current_position(), (controller.lock_point.x, controller.lock_point.y))

    async def test_remote_failure_returns_to_local_edge_when_possible(self) -> None:
        peer = PeerConfig(name="mac", edge="left")
        config = AppConfig(machine_name="host", peers=[peer])
        backend = TrackingBackend()
        state = StateStore("host", backend.name)
        controller = BorderController(config, backend, state)
        controller.active = ActiveRemote(peer=peer, client=FakeRemoteClient(), point=Point(1678, 486))
        state.update(active_peer="mac")
        controller._start_capture(suppress=True)

        await controller._recover_local("mac disconnected")

        self.assertEqual(backend.current_position(), (2, 500))


if __name__ == "__main__":
    unittest.main()
