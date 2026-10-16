from __future__ import annotations

import asyncio
import contextlib
import io
import json
import ssl
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mwbc.clipboard import NullClipboard
from mwbc.config import AppConfig, PeerConfig, load_config, save_config
from mwbc.cli import main
from mwbc.input_backend import NullBackend
from mwbc.network import (AgentServer, ClientConnector, HostClientRegistry, RemoteClient,
                          _client_session, read_message, send_message)
from mwbc.protocol import AuthenticationError, encode_message, session_secret
from mwbc.security import fingerprint, identity_bundle, initialize_identity, tls_context, trust_identity
from mwbc.state import StateStore


class SecurityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.handshake_errors = []
        loop = asyncio.get_running_loop()
        original_handler = loop.get_exception_handler()

        def expected_handshake_failure(event_loop, context):
            error = context.get("exception")
            if context.get("message") == "Error on transport creation for incoming connection" and isinstance(error, (ssl.SSLError, ConnectionResetError)):
                self.handshake_errors.append(error)
            else:
                event_loop.default_exception_handler(context)

        loop.set_exception_handler(expected_handshake_failure)
        self.addCleanup(loop.set_exception_handler, original_handler)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.host = self.make_device("host")
        self.client = self.make_device("client")
        self.other = self.make_device("other")
        self.host.peers = [PeerConfig(name="client", device_id=self.client.device_id, edge="left")]
        self.approve(self.host, self.client, ["client"])
        self.approve(self.client, self.host, ["controller"])
        self.state = StateStore("host", "null")
        self.state.register_peer("client", "", 45445, "left")
        self.registry = HostClientRegistry(self.state)
        self.server = AgentServer(self.host, NullBackend(), self.state, self.registry)
        await self.server.start()
        self.port = self.server._server.sockets[0].getsockname()[1]
        self.writers = []

    async def asyncTearDown(self) -> None:
        for writer in self.writers:
            writer.close()
            try:
                await writer.wait_closed()
            except (OSError, ConnectionError):
                pass
        await self.server.stop()
        await asyncio.sleep(0.02)

    def make_device(self, name: str) -> AppConfig:
        config = AppConfig(machine_name=name, pairing_secret="disposable-test-secret",
                           listen_host="127.0.0.1", listen_port=0, clipboard_enabled=False)
        initialize_identity(config, self.root / name / "config.json")
        return config

    def approve(self, local: AppConfig, remote: AppConfig, roles: list[str]) -> None:
        bundle = identity_bundle(remote)
        trust_identity(local, bundle, bundle["fingerprint"], roles)

    async def connect(self, config=None):
        config = config or self.client
        reader, writer = await asyncio.wait_for(asyncio.open_connection(
            "127.0.0.1", self.port, ssl=tls_context(config, server=False, host="127.0.0.1")), 2)
        self.writers.append(writer)
        await _client_session(reader, writer)
        return reader, writer

    async def hello(self, reader, writer, config=None, **overrides):
        config = config or self.client
        payload = {"role": "client", "machine_name": config.machine_name,
                   "device_id": config.device_id, "screen_width": 1920, "screen_height": 1080}
        payload.update(overrides)
        await send_message(writer, "hello", payload, config.pairing_secret)

    async def wait_registered(self):
        for _ in range(40):
            peer = await self.registry.get("client")
            if peer:
                return peer
            await asyncio.sleep(0.01)
        self.fail("client did not register")

    async def wait_removed(self):
        for _ in range(40):
            if await self.registry.get("client") is None:
                return
            await asyncio.sleep(0.01)
        self.fail("rejected client remained registered")

    async def test_mutual_tls_delivers_input(self) -> None:
        backend = NullBackend()
        client_state = StateStore("client", "null")
        connector = ClientConnector(self.client, backend, client_state, "127.0.0.1", self.port)
        await connector.start()
        try:
            peer = await self.wait_registered()
            connection = peer.writer.get_extra_info("ssl_object")
            self.assertIn(connection.version(), {"TLSv1.2", "TLSv1.3"})
            self.assertIsNotNone(connection.cipher())
            await peer.send("input", {"action": "move_relative", "dx": 9, "dy": 3})
            await asyncio.sleep(0.05)
            self.assertEqual(backend.current_position(), (969, 543))
        finally:
            await connector.stop()

    async def test_clipboard_image_transfer_over_tls(self) -> None:
        from mwbc.clipboard import ClipboardPayload

        self.host.clipboard_enabled = True
        self.client.clipboard_enabled = True
        target = NullClipboard()
        connector = ClientConnector(self.client, NullBackend(), StateStore("client", "null"),
                                    "127.0.0.1", self.port, clipboard=target)
        await connector.start()
        try:
            peer = await self.wait_registered()
            image = b"\x89PNG\r\n\x1a\n" + b"x" * 90000
            # Existing image validation is intentionally unchanged by transport hardening.
            payload = ClipboardPayload(kind="image", data=image)
            await peer.send("clipboard", payload.to_wire_payload("host"))
            for _ in range(50):
                if target.get_image_png() == image:
                    break
                await asyncio.sleep(0.01)
            self.assertEqual(target.get_image_png(), image)
        finally:
            await connector.stop()

    async def test_untrusted_client_certificate_is_rejected(self) -> None:
        self.approve(self.other, self.host, ["controller"])
        with self.assertRaises((ssl.SSLError, ConnectionError, asyncio.TimeoutError, AuthenticationError)):
            await self.connect(self.other)
        self.assertIsNone(await self.registry.get("other"))

    async def test_client_without_certificate_is_rejected(self) -> None:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.load_verify_locations(cadata=identity_bundle(self.host)["certificate"])
        with self.assertRaises((ssl.SSLError, ConnectionError, AuthenticationError)):
            reader, writer = await asyncio.open_connection("127.0.0.1", self.port, ssl=context)
            self.writers.append(writer)
            await _client_session(reader, writer)

    async def test_untrusted_server_certificate_is_rejected(self) -> None:
        self.client.trusted_devices = []
        with self.assertRaises(ssl.SSLCertVerificationError):
            await self.connect()

    async def test_plaintext_connection_cannot_receive_session_challenge(self) -> None:
        reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
        self.writers.append(writer)
        writer.write(b"plaintext-not-TLS\n")
        await writer.drain()
        self.assertEqual(await asyncio.wait_for(reader.read(256), 2), b"")

    async def test_client_cannot_claim_controller_role(self) -> None:
        reader, writer = await self.connect()
        await self.hello(reader, writer, role="controller")
        self.assertEqual(await asyncio.wait_for(reader.read(), 2), b"")
        self.assertIsNone(self.server._controller_writer)

    async def test_host_rejects_native_controller_even_if_role_is_approved(self) -> None:
        self.host.trusted_devices[0]["roles"].append("controller")
        reader, writer = await self.connect()
        await self.hello(reader, writer, role="controller")
        self.assertEqual(await asyncio.wait_for(reader.read(), 2), b"")
        self.assertIsNone(self.server._controller_writer)

    async def test_unknown_role_is_rejected(self) -> None:
        reader, writer = await self.connect()
        await self.hello(reader, writer, role="administrator")
        self.assertEqual(await asyncio.wait_for(reader.read(), 2), b"")

    async def test_claimed_name_must_match_approved_certificate(self) -> None:
        reader, writer = await self.connect()
        await self.hello(reader, writer, machine_name="other")
        self.assertEqual(await asyncio.wait_for(reader.read(), 2), b"")

    async def test_certificate_binds_claimed_device_identity(self) -> None:
        reader, writer = await self.connect()
        await self.hello(reader, writer, device_id=self.other.device_id)
        self.assertEqual(await asyncio.wait_for(reader.read(), 2), b"")
        self.assertIsNone(await self.registry.get("client"))

    async def test_client_role_cannot_inject_input_into_host(self) -> None:
        reader, writer = await self.connect()
        await self.hello(reader, writer)
        await read_message(reader, self.client.pairing_secret)
        await self.wait_registered()
        await send_message(writer, "input", {"action": "move", "x": 1, "y": 1}, self.client.pairing_secret)
        await self.wait_removed()
        self.assertEqual(self.server.backend.current_position(), (960, 540))
        self.assertEqual(self.state.snapshot()["events_received"], 0)

    async def test_repeated_frame_is_rejected_in_same_session(self) -> None:
        reader, writer = await self.connect()
        await self.hello(reader, writer)
        await read_message(reader, self.client.pairing_secret)
        await self.wait_registered()
        frame = encode_message("heartbeat", {}, session_secret(self.client.pairing_secret, writer._mwbc_challenge), sequence=2)
        writer.write(frame + frame)
        await writer.drain()
        await self.wait_removed()

    async def test_frame_cannot_be_replayed_in_new_session(self) -> None:
        reader, writer = await self.connect()
        frame = encode_message("hello", {"role": "client", "machine_name": "client", "device_id": self.client.device_id},
                               session_secret(self.client.pairing_secret, writer._mwbc_challenge), sequence=1)
        reader2, writer2 = await self.connect()
        self.assertNotEqual(writer._mwbc_challenge, writer2._mwbc_challenge)
        writer2.write(frame)
        await writer2.drain()
        self.assertEqual(await asyncio.wait_for(reader2.read(), 2), b"")

    async def test_missing_sequence_is_rejected(self) -> None:
        reader, writer = await self.connect()
        writer.write(encode_message("hello", {}, session_secret(self.client.pairing_secret, writer._mwbc_challenge)))
        await writer.drain()
        self.assertEqual(await asyncio.wait_for(reader.read(), 2), b"")

    async def test_skipped_sequence_is_rejected(self) -> None:
        reader, writer = await self.connect()
        await self.hello(reader, writer)
        await read_message(reader, self.client.pairing_secret)
        await self.wait_registered()
        writer.write(encode_message("heartbeat", {}, session_secret(self.client.pairing_secret, writer._mwbc_challenge), sequence=3))
        await writer.drain()
        await self.wait_removed()

    async def test_host_frame_sequence_is_checked_by_client_reader(self) -> None:
        reader, writer = await self.connect()
        await self.hello(reader, writer)
        await read_message(reader, self.client.pairing_secret)
        peer = await self.wait_registered()
        # Settings is the second server frame; consume it before replaying it.
        await read_message(reader, self.client.pairing_secret)
        replay = encode_message("settings", {}, session_secret(self.client.pairing_secret, writer._mwbc_challenge), sequence=2)
        peer.writer.write(replay)
        await peer.writer.drain()
        with self.assertRaises(AuthenticationError):
            await read_message(reader, self.client.pairing_secret)

    async def test_only_one_native_controller_can_own_agent(self) -> None:
        class RecordingBackend(NullBackend):
            resets = 0

            def reset_input(self):
                self.resets += 1
                super().reset_input()

        agent_config = self.make_device("agent")
        self.approve(agent_config, self.client, ["controller"])
        self.approve(agent_config, self.other, ["controller"])
        self.approve(self.client, agent_config, ["agent"])
        self.approve(self.other, agent_config, ["agent"])
        agent = AgentServer(agent_config, RecordingBackend(), StateStore("agent", "null"))
        await agent.start()
        port = agent._server.sockets[0].getsockname()[1]
        first = RemoteClient(self.client, PeerConfig(name="agent", host="127.0.0.1", port=port), NullBackend(), self.state)
        second = RemoteClient(self.other, PeerConfig(name="agent", host="127.0.0.1", port=port), NullBackend(), self.state)
        try:
            await first.connect()
            await first.send("input", {"action": "move", "x": 31, "y": 32})
            with self.assertRaises(ConnectionError):
                await second.connect()
            self.assertIsNotNone(agent._controller_writer)
            await asyncio.sleep(0.02)
            self.assertEqual(agent.backend.current_position(), (31, 32))
            self.assertEqual(agent.backend.resets, 0)
        finally:
            await first.close()
            await second.close()
            await agent.stop()

    async def test_replayed_input_is_applied_only_once(self) -> None:
        target = self.make_device("target")
        self.approve(target, self.client, ["controller"])
        self.approve(self.client, target, ["agent"])
        state = StateStore("target", "null")
        agent = AgentServer(target, NullBackend(), state)
        await agent.start()
        port = agent._server.sockets[0].getsockname()[1]
        controller = RemoteClient(self.client, PeerConfig(name="target", host="127.0.0.1", port=port), NullBackend(), self.state)
        try:
            await controller.connect()
            writer = controller.writer
            frame = encode_message("input", {"action": "move_relative", "dx": 10, "dy": 0},
                                   session_secret(self.client.pairing_secret, writer._mwbc_challenge), sequence=2)
            writer.write(frame + frame)
            await writer.drain()
            await asyncio.sleep(0.05)
            self.assertEqual(state.snapshot()["events_received"], 1)
            self.assertEqual(agent.backend.current_position(), (970, 540))
        finally:
            await controller.close()
            await agent.stop()

    def test_fingerprint_requires_explicit_correct_approval(self) -> None:
        with self.assertRaises(AuthenticationError):
            trust_identity(self.host, identity_bundle(self.other), "0" * 64, ["client"])

    def test_changed_certificate_cannot_silently_replace_trusted_identity(self) -> None:
        replacement = AppConfig(machine_name="client", device_id=self.client.device_id)
        initialize_identity(replacement, self.root / "replacement" / "config.json")
        with self.assertRaisesRegex(AuthenticationError, "revoke"):
            self.approve(self.host, replacement, ["client"])

    def test_cli_public_export_trust_and_revoke(self) -> None:
        host_path = self.root / "host" / "config.json"
        client_path = self.root / "client" / "config.json"
        exported = self.root / "client-public.json"
        with contextlib.redirect_stdout(io.StringIO()), patch("mwbc.cli.logging.basicConfig"), patch("mwbc.cli.install_recent_log_handler"):
            self.assertEqual(main(["--config", str(client_path), "security", "export", "--output", str(exported)]), 0)
            self.assertEqual(main(["--config", str(host_path), "security", "trust", "--file", str(exported),
                                   "--fingerprint", identity_bundle(self.client)["fingerprint"], "--role", "client"]), 0)
            approved = load_config(host_path)
            self.assertEqual(approved.trusted_devices[0]["device_id"], self.client.device_id)
            self.assertEqual(main(["--config", str(host_path), "security", "revoke", "--device-id", self.client.device_id]), 0)
        self.assertEqual(load_config(host_path).trusted_devices, [])

    def test_management_token_is_not_a_peer_secret(self) -> None:
        self.assertNotEqual(self.host.api_token, self.host.pairing_secret)
        self.assertNotIn(self.host.api_token, self.host.accepted_secrets())

    def test_init_does_not_overwrite_existing_identity(self) -> None:
        prior = identity_bundle(self.host)
        with self.assertRaisesRegex(ValueError, "already configured"):
            initialize_identity(self.host, self.root / "host" / "config.json")
        self.assertEqual(identity_bundle(self.host), prior)

    def test_export_excludes_private_key_and_tokens(self) -> None:
        exported = json.dumps(identity_bundle(self.host))
        self.assertNotIn("PRIVATE KEY", exported)
        self.assertNotIn(self.host.pairing_secret, exported)
        self.assertNotIn(self.host.api_token, exported)

    def test_tls_is_default_and_never_falls_back(self) -> None:
        with self.assertRaisesRegex(ValueError, "TLS identity missing"):
            tls_context(AppConfig(), server=True, host="127.0.0.1")
        with self.assertRaisesRegex(ValueError, "loopback"):
            tls_context(AppConfig(allow_insecure_loopback=True), server=True, host="0.0.0.0")

    def test_identity_and_trust_survive_config_roundtrip(self) -> None:
        path = self.root / "roundtrip.json"
        save_config(self.host, path)
        restored = load_config(path)
        self.assertEqual(restored.trusted_devices, self.host.trusted_devices)
        self.assertEqual(fingerprint(identity_bundle(restored)["certificate"]), identity_bundle(self.host)["fingerprint"])
