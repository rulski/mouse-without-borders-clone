from __future__ import annotations

import unittest

from mwbc.config import AppConfig, PeerConfig
from mwbc.controller import BorderController
from mwbc.input_backend import NullBackend
from mwbc.state import StateStore


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


if __name__ == "__main__":
    unittest.main()
