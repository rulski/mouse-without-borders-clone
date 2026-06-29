from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace

from mwbc.config import AppConfig, PeerConfig
from mwbc.controller import ActiveRemote, BorderController
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
        self.assertEqual(backend.current_position(), (960, 540))
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


if __name__ == "__main__":
    unittest.main()
