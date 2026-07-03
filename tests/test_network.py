from __future__ import annotations

import asyncio
import unittest

from mwbc.clipboard import NullClipboard
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

    async def test_always_looking_client_sends_clipboard_to_host(self) -> None:
        host_config = AppConfig(
            machine_name="host",
            pairing_secret="secret",
            listen_host="127.0.0.1",
            listen_port=0,
            clipboard_poll_seconds=0.05,
            peers=[PeerConfig(name="client", edge="right")],
        )
        host_backend = NullBackend()
        host_clipboard = NullClipboard()
        host_state = StateStore("host", host_backend.name)
        host_state.register_peer("client", "", 45445, "right")
        registry = HostClientRegistry(host_state)
        server = AgentServer(
            host_config,
            host_backend,
            host_state,
            host_registry=registry,
            clipboard=host_clipboard,
        )
        await server.start()
        assert server._server is not None
        port = server._server.sockets[0].getsockname()[1]

        client_config = AppConfig(
            machine_name="client",
            pairing_secret="secret",
            clipboard_poll_seconds=0.05,
        )
        client_backend = NullBackend()
        client_clipboard = NullClipboard("initial")
        client_state = StateStore("client", client_backend.name)
        connector = ClientConnector(
            config=client_config,
            backend=client_backend,
            state=client_state,
            host="127.0.0.1",
            port=port,
            retry_seconds=0.05,
            clipboard=client_clipboard,
        )

        try:
            await connector.start()
            await self._wait_for_hosted_client(registry, "client")
            client_clipboard.set_text("copied from client")
            await self._wait_for_clipboard(host_clipboard, "copied from client")
        finally:
            await connector.stop()
            await server.stop()

    async def test_host_sends_clipboard_to_always_looking_client(self) -> None:
        host_config = AppConfig(
            machine_name="host",
            pairing_secret="secret",
            listen_host="127.0.0.1",
            listen_port=0,
            clipboard_poll_seconds=0.05,
            peers=[PeerConfig(name="client", edge="right")],
        )
        host_backend = NullBackend()
        host_clipboard = NullClipboard("initial host")
        host_state = StateStore("host", host_backend.name)
        host_state.register_peer("client", "", 45445, "right")
        registry = HostClientRegistry(host_state)
        server = AgentServer(
            host_config,
            host_backend,
            host_state,
            host_registry=registry,
            clipboard=host_clipboard,
        )
        await server.start()
        assert server._server is not None
        port = server._server.sockets[0].getsockname()[1]

        client_config = AppConfig(
            machine_name="client",
            pairing_secret="secret",
            clipboard_poll_seconds=0.05,
        )
        client_backend = NullBackend()
        client_clipboard = NullClipboard("initial client")
        client_state = StateStore("client", client_backend.name)
        connector = ClientConnector(
            config=client_config,
            backend=client_backend,
            state=client_state,
            host="127.0.0.1",
            port=port,
            retry_seconds=0.05,
            clipboard=client_clipboard,
        )

        try:
            await connector.start()
            await self._wait_for_hosted_client(registry, "client")
            host_clipboard.set_text("copied from host")
            await self._wait_for_clipboard(client_clipboard, "copied from host")
            await asyncio.sleep(0.2)
            self.assertEqual(host_state.snapshot().get("last_clipboard_source"), "host")
        finally:
            await connector.stop()
            await server.stop()

    async def test_client_connection_exits_when_heartbeat_fails(self) -> None:
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

        async def fail_heartbeats(writer, secret) -> None:
            await asyncio.sleep(0.01)
            raise ConnectionError("heartbeat failed")

        connector._running = True
        connector._send_heartbeats = fail_heartbeats  # type: ignore[method-assign]

        try:
            with self.assertRaisesRegex(ConnectionError, "heartbeat failed"):
                await asyncio.wait_for(connector._connect_once(), timeout=1)
            self.assertFalse(client_state.snapshot()["host_connected"])
        finally:
            connector._running = False
            await server.stop()

    async def test_host_sends_keep_awake_settings_to_client(self) -> None:
        host_config = AppConfig(
            machine_name="host",
            pairing_secret="secret",
            listen_host="127.0.0.1",
            listen_port=0,
            peers=[
                PeerConfig(
                    name="client",
                    edge="right",
                    keep_awake=True,
                    keep_awake_interval_seconds=0.1,
                )
            ],
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
            await self._wait_for_hosted_client(registry, "client")
            await self._wait_for_state(client_state, "keep_awake", True)
            await self._wait_for_state(client_state, "last_keep_awake_at")
            self.assertEqual(client_backend.current_position(), (960, 540))
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

    async def _wait_for_clipboard(self, clipboard: NullClipboard, expected: str) -> None:
        for _ in range(60):
            if clipboard.get_text() == expected:
                return
            await asyncio.sleep(0.05)
        self.fail(f"clipboard did not become {expected!r}")

    async def _wait_for_state(self, state: StateStore, key: str, expected=None) -> None:
        for _ in range(60):
            value = state.snapshot().get(key)
            if expected is None and value is not None:
                return
            if expected is not None and value == expected:
                return
            await asyncio.sleep(0.05)
        self.fail(f"state {key!r} did not become {expected!r}")


if __name__ == "__main__":
    unittest.main()
