from __future__ import annotations

import asyncio
import unittest

from mwbc.config import AppConfig, PeerConfig
from mwbc.input_backend import NullBackend
from mwbc.network import AgentServer, ClientConnector, HostClientRegistry
from mwbc.state import StateStore


class NetworkTests(unittest.IsolatedAsyncioTestCase):
    async def test_always_looking_client_receives_host_input(self) -> None:
        host_config = AppConfig(
            machine_name="host",
            pairing_secret="secret",
            listen_host="127.0.0.1",
            listen_port=0,
            peers=[PeerConfig(name="client", edge="right")],
        )
        host_backend = NullBackend()
        host_state = StateStore("host", host_backend.name)
        host_state.register_peer("client", "", 45445, "right")
        registry = HostClientRegistry(host_state)
        server = AgentServer(host_config, host_backend, host_state, host_registry=registry)
        await server.start()
        assert server._server is not None
        port = server._server.sockets[0].getsockname()[1]

        client_config = AppConfig(machine_name="client", pairing_secret="secret")
        client_backend = NullBackend()
        client_state = StateStore("client", client_backend.name)
        connector = ClientConnector(
            config=client_config,
            backend=client_backend,
            state=client_state,
            host="127.0.0.1",
            port=port,
            retry_seconds=0.05,
        )

        try:
            await connector.start()
            hosted_client = await self._wait_for_hosted_client(registry, "client")
            await hosted_client.send("input", {"action": "move_relative", "dx": 10, "dy": -5})
            await asyncio.sleep(0.05)
            self.assertEqual(client_backend.current_position(), (970, 535))
            self.assertTrue(client_state.snapshot()["host_connected"])
        finally:
            await connector.stop()
            await server.stop()

    async def _wait_for_hosted_client(self, registry: HostClientRegistry, name: str):
        for _ in range(60):
            client = await registry.get(name)
            if client is not None:
                return client
            await asyncio.sleep(0.05)
        self.fail(f"{name} did not connect")


if __name__ == "__main__":
    unittest.main()

