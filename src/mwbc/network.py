from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from .clipboard import Clipboard, ClipboardError, truncate_text
from .config import AppConfig, PeerConfig
from .input_backend import InputBackend, apply_input_event
from .protocol import AuthenticationError, Message, decode_message, decode_with_any_secret, encode_message
from .state import StateStore

logger = logging.getLogger(__name__)

MessageHandler = Callable[[Message, str], Awaitable[None]]


async def send_message(writer: asyncio.StreamWriter, message_type: str, payload: dict[str, Any], secret: str) -> None:
    writer.write(encode_message(message_type, payload, secret))
    await writer.drain()


async def read_message(reader: asyncio.StreamReader, secret: str) -> Message:
    line = await reader.readline()
    if not line:
        raise ConnectionError("connection closed")
    return decode_message(line, secret)


@dataclass(slots=True)
class RemoteScreen:
    width: int
    height: int


def _screen_from_payload(payload: dict[str, Any]) -> RemoteScreen:
    return RemoteScreen(
        width=int(payload.get("screen_width", 1920)),
        height=int(payload.get("screen_height", 1080)),
    )


class RemoteClient:
    def __init__(
        self,
        config: AppConfig,
        peer: PeerConfig,
        backend: InputBackend,
        state: StateStore,
    ) -> None:
        self.config = config
        self.peer = peer
        self.backend = backend
        self.state = state
        self.reader: asyncio.StreamReader | None = None
        self.writer: asyncio.StreamWriter | None = None
        self.remote_screen = RemoteScreen(width=1920, height=1080)
        self._secret = config.secret_for_peer(peer)

    @property
    def connected(self) -> bool:
        return self.writer is not None and not self.writer.is_closing()

    async def connect(self) -> None:
        if self.connected:
            return
        if not self.peer.host:
            raise ConnectionError(f"{self.peer.name} has no direct host address; waiting for it to connect")

        reader, writer = await asyncio.open_connection(self.peer.host, self.peer.port)
        self.reader = reader
        self.writer = writer
        width, height = self.backend.screen_size()
        await send_message(
            writer,
            "hello",
            {
                "machine_name": self.config.machine_name,
                "screen_width": width,
                "screen_height": height,
                "client_version": "0.1.0",
            },
            self._secret,
        )
        ack = await read_message(reader, self._secret)
        if ack.type != "hello_ack":
            raise ConnectionError(f"expected hello_ack, got {ack.type!r}")

        self.remote_screen = _screen_from_payload(ack.payload)
        self.state.update_peer(
            self.peer.name,
            connected=True,
            screen_width=self.remote_screen.width,
            screen_height=self.remote_screen.height,
            last_seen=time.time(),
            error=None,
        )

    async def send(self, message_type: str, payload: dict[str, Any]) -> None:
        if not self.connected:
            await self.connect()
        assert self.writer is not None
        await send_message(self.writer, message_type, payload, self._secret)
        self.state.update_peer(self.peer.name, last_seen=time.time(), error=None)

    async def close(self) -> None:
        writer = self.writer
        self.reader = None
        self.writer = None
        if writer is not None:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
        self.state.update_peer(self.peer.name, connected=False)


class HostedRemoteClient:
    """A target/client that dialed into this host and can receive input events."""

    def __init__(
        self,
        name: str,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        secret: str,
        remote_screen: RemoteScreen,
        state: StateStore,
    ) -> None:
        self.name = name
        self.reader = reader
        self.writer = writer
        self._secret = secret
        self.remote_screen = remote_screen
        self.state = state

    @property
    def connected(self) -> bool:
        return not self.writer.is_closing()

    async def connect(self) -> None:
        if not self.connected:
            raise ConnectionError(f"{self.name} is not connected to this host")

    async def send(self, message_type: str, payload: dict[str, Any]) -> None:
        if not self.connected:
            raise ConnectionError(f"{self.name} is not connected to this host")
        await send_message(self.writer, message_type, payload, self._secret)
        self.state.update_peer(self.name, last_seen=time.time(), error=None)

    async def close(self) -> None:
        if not self.writer.is_closing():
            self.writer.close()
            with contextlib.suppress(Exception):
                await self.writer.wait_closed()
        self.state.update_peer(self.name, connected=False)


class HostClientRegistry:
    def __init__(self, state: StateStore) -> None:
        self.state = state
        self._clients: dict[str, HostedRemoteClient] = {}
        self._lock = asyncio.Lock()

    async def register(self, client: HostedRemoteClient) -> None:
        old_client: HostedRemoteClient | None = None
        async with self._lock:
            old_client = self._clients.get(client.name)
        if old_client is not None and old_client is not client:
            await old_client.close()

        async with self._lock:
            self._clients[client.name] = client

        self.state.update_peer(
            client.name,
            connected=True,
            screen_width=client.remote_screen.width,
            screen_height=client.remote_screen.height,
            last_seen=time.time(),
            error=None,
        )

    async def unregister(self, name: str, client: HostedRemoteClient) -> None:
        async with self._lock:
            if self._clients.get(name) is client:
                self._clients.pop(name, None)
                self.state.update_peer(name, connected=False)

    async def get(self, name: str) -> HostedRemoteClient | None:
        async with self._lock:
            client = self._clients.get(name)
        if client is not None and client.connected:
            return client
        return None

    async def connected_clients(self) -> list[HostedRemoteClient]:
        async with self._lock:
            clients = list(self._clients.values())
        return [client for client in clients if client.connected]

    async def close_all(self) -> None:
        async with self._lock:
            clients = list(self._clients.values())
            self._clients.clear()
        for client in clients:
            await client.close()


class AgentServer:
    def __init__(
        self,
        config: AppConfig,
        backend: InputBackend,
        state: StateStore,
        host_registry: HostClientRegistry | None = None,
        clipboard: Clipboard | None = None,
    ) -> None:
        self.config = config
        self.backend = backend
        self.state = state
        self.host_registry = host_registry
        self.clipboard = clipboard
        self._server: asyncio.AbstractServer | None = None
        self._clipboard_task: asyncio.Task[None] | None = None
        self._clipboard_last_seen: str | None = None

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle_client, self.config.listen_host, self.config.listen_port)
        sockets = ", ".join(str(sock.getsockname()) for sock in (self._server.sockets or []))
        logger.info("agent listening on %s", sockets)
        if (
            self.host_registry is not None
            and self.clipboard is not None
            and self.config.clipboard_enabled
            and self._clipboard_task is None
        ):
            self._clipboard_task = asyncio.create_task(
                self._send_host_clipboard_changes(),
                name="mwbc-host-clipboard",
            )

    async def serve_forever(self) -> None:
        if self._server is None:
            await self.start()
        assert self._server is not None
        async with self._server:
            await self._server.serve_forever()

    async def stop(self) -> None:
        if self._clipboard_task is not None:
            self._clipboard_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._clipboard_task
            self._clipboard_task = None
        if self._server is None:
            return
        self._server.close()
        await self._server.wait_closed()
        self._server = None
        if self.host_registry is not None:
            await self.host_registry.close_all()

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peername = str(writer.get_extra_info("peername"))
        incoming_key = peername
        secret = ""
        try:
            hello_line = await reader.readline()
            if not hello_line:
                return
            hello, secret = decode_with_any_secret(hello_line, self.config.accepted_secrets())
            if hello.type != "hello":
                raise AuthenticationError("first frame must be hello")

            client_name = str(hello.payload.get("machine_name", peername))
            role = str(hello.payload.get("role", "controller"))
            incoming_key = f"{client_name} {peername}"
            self.state.update_incoming(
                incoming_key,
                machine_name=client_name,
                peer=peername,
                connected=True,
                screen_width=hello.payload.get("screen_width"),
                screen_height=hello.payload.get("screen_height"),
                last_seen=time.time(),
            )

            width, height = self.backend.screen_size()
            await send_message(
                writer,
                "hello_ack",
                {
                    "machine_name": self.config.machine_name,
                    "screen_width": width,
                    "screen_height": height,
                    "server_version": "0.1.0",
                },
                secret,
            )

            if role == "client":
                if self.host_registry is None:
                    raise ConnectionError("this machine is not running as a host")
                hosted_client = HostedRemoteClient(
                    name=client_name,
                    reader=reader,
                    writer=writer,
                    secret=secret,
                    remote_screen=_screen_from_payload(hello.payload),
                    state=self.state,
                )
                await self.host_registry.register(hosted_client)
                await self._send_client_settings(hosted_client)
                await self._monitor_hosted_client(hosted_client, incoming_key)
                return

            while True:
                message = await read_message(reader, secret)
                self.state.update_incoming(incoming_key, last_seen=time.time())
                await self._handle_message(message, incoming_key)
        except (AuthenticationError, ConnectionError, asyncio.IncompleteReadError) as exc:
            logger.info("client %s disconnected: %s", peername, exc)
        except Exception as exc:
            logger.exception("client %s failed", peername)
            self.state.set_error(str(exc))
        finally:
            self.state.remove_incoming(incoming_key)
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    async def _monitor_hosted_client(self, client: HostedRemoteClient, incoming_key: str) -> None:
        try:
            while True:
                message = await read_message(client.reader, client._secret)
                self.state.update_incoming(incoming_key, last_seen=time.time())
                self.state.update_peer(client.name, last_seen=time.time(), error=None)
                if message.type == "heartbeat":
                    continue
                if message.type == "clipboard":
                    await self._apply_clipboard_message(message, client.name)
                    continue
                logger.debug("ignoring hosted client message type %s from %s", message.type, client.name)
        finally:
            if self.host_registry is not None:
                await self.host_registry.unregister(client.name, client)

    async def _handle_message(self, message: Message, incoming_key: str) -> None:
        if message.type == "input":
            apply_input_event(self.backend, message.payload)
            self.state.increment("events_received")
        elif message.type == "control":
            self.state.update_incoming(incoming_key, active=message.payload.get("active"))
        elif message.type == "clipboard":
            await self._apply_clipboard_message(message, incoming_key)
        elif message.type == "heartbeat":
            return
        else:
            logger.warning("ignoring unknown message type %s", message.type)

    async def _apply_clipboard_message(self, message: Message, source: str) -> None:
        if self.clipboard is None or not self.config.clipboard_enabled:
            return
        if "text" not in message.payload:
            raise ValueError("clipboard message missing text")

        text = truncate_text(str(message.payload["text"]), self.config.clipboard_max_text_bytes)
        await asyncio.to_thread(self.clipboard.set_text, text)
        self._clipboard_last_seen = text
        self.state.update(last_clipboard_source=source, last_clipboard_at=time.time())
        await self._broadcast_clipboard(text, exclude=source)

    async def _send_host_clipboard_changes(self) -> None:
        assert self.clipboard is not None
        try:
            self._clipboard_last_seen = truncate_text(
                await self._read_clipboard_text(),
                self.config.clipboard_max_text_bytes,
            )
        except ClipboardError as exc:
            self._clipboard_last_seen = ""
            self.state.update(clipboard_error=str(exc))

        while True:
            await asyncio.sleep(max(0.1, self.config.clipboard_poll_seconds))
            try:
                text = truncate_text(
                    await self._read_clipboard_text(),
                    self.config.clipboard_max_text_bytes,
                )
            except ClipboardError as exc:
                self.state.update(clipboard_error=str(exc))
                continue
            if text == self._clipboard_last_seen:
                continue
            self._clipboard_last_seen = text
            await self._broadcast_clipboard(text)
            self.state.update(
                clipboard_error=None,
                last_clipboard_source=self.config.machine_name,
                last_clipboard_sent=time.time(),
            )

    async def _read_clipboard_text(self) -> str:
        assert self.clipboard is not None
        return await asyncio.to_thread(self.clipboard.get_text)

    async def _broadcast_clipboard(self, text: str, *, exclude: str | None = None) -> None:
        if self.host_registry is None or not self.config.clipboard_enabled:
            return
        payload = {
            "source": self.config.machine_name,
            "text": truncate_text(text, self.config.clipboard_max_text_bytes),
        }
        for client in await self.host_registry.connected_clients():
            if exclude is not None and client.name == exclude:
                continue
            try:
                await client.send("clipboard", payload)
            except Exception as exc:
                logger.info("failed to send clipboard to %s: %s", client.name, exc)
                self.state.update_peer(client.name, error=str(exc))

    async def _send_client_settings(self, client: HostedRemoteClient) -> None:
        settings = {
            "keep_awake": False,
            "keep_awake_interval_seconds": 45.0,
        }
        for peer in self.config.peers:
            if peer.name == client.name:
                settings = peer.client_settings()
                break
        await client.send("settings", settings)


class ClientConnector:
    def __init__(
        self,
        config: AppConfig,
        backend: InputBackend,
        state: StateStore,
        host: str,
        port: int,
        retry_seconds: float = 1.0,
        clipboard: Clipboard | None = None,
    ) -> None:
        self.config = config
        self.backend = backend
        self.state = state
        self.host = host
        self.port = port
        self.retry_seconds = retry_seconds
        self.clipboard = clipboard
        self._running = False
        self._task: asyncio.Task[None] | None = None
        self._host_active = False
        self._keep_awake = False
        self._keep_awake_interval_seconds = 45.0
        self._settings_changed = asyncio.Event()
        self._clipboard_last_seen: str | None = None

    async def start(self) -> None:
        if self._task is not None:
            return
        self._running = True
        self._task = asyncio.create_task(self._run(), name="mwbc-client-connector")

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _run(self) -> None:
        while self._running:
            try:
                await self._connect_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.info("host connection failed: %s", exc)
                self.state.update(host_connected=False, host_error=str(exc))
            if self._running:
                await asyncio.sleep(self.retry_seconds)

    async def _connect_once(self) -> None:
        reader, writer = await asyncio.open_connection(self.host, self.port)
        secret = self.config.pairing_secret
        width, height = self.backend.screen_size()
        await send_message(
            writer,
            "hello",
            {
                "role": "client",
                "machine_name": self.config.machine_name,
                "screen_width": width,
                "screen_height": height,
                "client_version": "0.1.0",
            },
            secret,
        )
        ack = await read_message(reader, secret)
        if ack.type != "hello_ack":
            raise ConnectionError(f"expected hello_ack, got {ack.type!r}")

        self.state.update(
            host_connected=True,
            host_error=None,
            host_name=ack.payload.get("machine_name"),
            host_screen_width=ack.payload.get("screen_width"),
            host_screen_height=ack.payload.get("screen_height"),
        )
        heartbeat_task = asyncio.create_task(self._send_heartbeats(writer, secret), name="mwbc-client-heartbeat")
        clipboard_task: asyncio.Task[None] | None = None
        keep_awake_task = asyncio.create_task(self._keep_awake_loop(), name="mwbc-client-keep-awake")
        if self.clipboard is not None and self.config.clipboard_enabled:
            clipboard_task = asyncio.create_task(
                self._send_clipboard_changes(writer, secret),
                name="mwbc-client-clipboard",
            )
        try:
            while self._running:
                message = await read_message(reader, secret)
                await self._handle_host_message(message)
        finally:
            heartbeat_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat_task
            if clipboard_task is not None:
                clipboard_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await clipboard_task
            keep_awake_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await keep_awake_task
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
            self._host_active = False
            self.state.update(host_connected=False, host_active=False)

    async def _send_heartbeats(self, writer: asyncio.StreamWriter, secret: str) -> None:
        while self._running and not writer.is_closing():
            await asyncio.sleep(3)
            await send_message(writer, "heartbeat", {}, secret)

    async def _keep_awake_loop(self) -> None:
        while self._running:
            try:
                await asyncio.wait_for(
                    self._settings_changed.wait(),
                    timeout=max(0.1, self._keep_awake_interval_seconds),
                )
                self._settings_changed.clear()
                continue
            except asyncio.TimeoutError:
                pass
            if not self._keep_awake or self._host_active:
                continue
            self.backend.move_relative(1, 0)
            await asyncio.sleep(0.05)
            self.backend.move_relative(-1, 0)
            self.state.update(last_keep_awake_at=time.time())

    async def _send_clipboard_changes(self, writer: asyncio.StreamWriter, secret: str) -> None:
        assert self.clipboard is not None
        try:
            self._clipboard_last_seen = truncate_text(
                await self._read_clipboard_text(),
                self.config.clipboard_max_text_bytes,
            )
        except ClipboardError as exc:
            self._clipboard_last_seen = ""
            self.state.update(clipboard_error=str(exc))
        while self._running and not writer.is_closing():
            await asyncio.sleep(max(0.1, self.config.clipboard_poll_seconds))
            try:
                text = await self._read_clipboard_text()
            except ClipboardError as exc:
                self.state.update(clipboard_error=str(exc))
                continue
            text = truncate_text(text, self.config.clipboard_max_text_bytes)
            if text == self._clipboard_last_seen:
                continue
            self._clipboard_last_seen = text
            payload = {
                "source": self.config.machine_name,
                "text": text,
            }
            await send_message(writer, "clipboard", payload, secret)
            self.state.update(clipboard_error=None, last_clipboard_sent=time.time())

    async def _read_clipboard_text(self) -> str:
        assert self.clipboard is not None
        return await asyncio.to_thread(self.clipboard.get_text)

    async def _handle_host_message(self, message: Message) -> None:
        if message.type == "input":
            apply_input_event(self.backend, message.payload)
            self.state.increment("events_received")
        elif message.type == "control":
            self._host_active = bool(message.payload.get("active"))
            self.state.update(host_active=self._host_active)
        elif message.type == "clipboard":
            if self.clipboard is not None and self.config.clipboard_enabled:
                text = truncate_text(str(message.payload.get("text", "")), self.config.clipboard_max_text_bytes)
                await asyncio.to_thread(self.clipboard.set_text, text)
                self._clipboard_last_seen = text
                self.state.update(last_clipboard_received=time.time())
        elif message.type == "settings":
            self._apply_settings(message.payload)
        elif message.type == "heartbeat":
            return
        else:
            logger.warning("ignoring unknown host message type %s", message.type)

    def _apply_settings(self, payload: dict[str, Any]) -> None:
        interval = float(payload.get("keep_awake_interval_seconds", 45.0))
        self._keep_awake = bool(payload.get("keep_awake", False))
        self._keep_awake_interval_seconds = min(3600.0, max(0.1, interval))
        self.state.update(
            keep_awake=self._keep_awake,
            keep_awake_interval_seconds=self._keep_awake_interval_seconds,
        )
        self._settings_changed.set()
