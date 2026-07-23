from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass
from typing import Any

from .config import AppConfig, PeerConfig
from .geometry import Point, Size, clamp, entry_position, local_exit_position, should_activate, should_exit
from .input_backend import CaptureCallbacks, InputBackend
from .network import HostClientRegistry, RemoteClient
from .state import StateStore

logger = logging.getLogger(__name__)
HOST_LOCK_HOTKEY = "F12"
LOCK_MOTION_DROP_SECONDS = 0.2
MAX_LOCK_DELTA_RATIO = 0.25


@dataclass(slots=True)
class ActiveRemote:
    peer: PeerConfig
    client: object
    point: Point


class BorderController:
    def __init__(
        self,
        config: AppConfig,
        backend: InputBackend,
        state: StateStore,
        host_registry: HostClientRegistry | None = None,
    ) -> None:
        self.config = config
        self.backend = backend
        self.state = state
        self.host_registry = host_registry
        self.loop: asyncio.AbstractEventLoop | None = None
        self.queue: asyncio.Queue[tuple[str, tuple[Any, ...]]] = asyncio.Queue()
        self.clients: dict[str, RemoteClient] = {
            peer.name: RemoteClient(config=config, peer=peer, backend=backend, state=state)
            for peer in config.peers if peer.host
        }
        self.active: ActiveRemote | None = None
        self.local_size = Size(*backend.screen_size())
        self.lock_point = Point(max(1, self.local_size.width // 2), max(1, self.local_size.height // 2))
        self._event_task: asyncio.Task[None] | None = None
        self._reconnect_task: asyncio.Task[None] | None = None
        self._active_watch_task: asyncio.Task[None] | None = None
        self._ignore_next_lock_motion = False
        self._ignore_lock_motion_until = 0.0
        self.edge_switching_paused = False

        for peer in config.peers:
            state.register_peer(peer.name, peer.host, peer.port, peer.edge)
        state.update(
            local_screen_width=self.local_size.width,
            local_screen_height=self.local_size.height,
            edge_switching_paused=self.edge_switching_paused,
            host_lock_hotkey=HOST_LOCK_HOTKEY,
        )

    async def start(self) -> None:
        self.loop = asyncio.get_running_loop()
        self._start_capture(suppress=False)
        self._event_task = asyncio.create_task(self._event_pump(), name="mwbc-event-pump")
        self._reconnect_task = asyncio.create_task(self._reconnect_loop(), name="mwbc-reconnect")
        self._active_watch_task = asyncio.create_task(self._active_watch_loop(), name="mwbc-active-watch")

    async def stop(self) -> None:
        self.backend.stop_capture()
        for task in (self._event_task, self._reconnect_task, self._active_watch_task):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        for client in self.clients.values():
            await client.close()

    async def refresh_peers(self) -> None:
        desired_direct = {peer.name for peer in self.config.peers if peer.host}
        for name in list(self.clients):
            if name not in desired_direct:
                client = self.clients.pop(name)
                await client.close()

        for peer in self.config.peers:
            self.state.register_peer(peer.name, peer.host, peer.port, peer.edge)
            if not peer.host:
                continue
            client = self.clients.get(peer.name)
            if client is None:
                self.clients[peer.name] = RemoteClient(config=self.config, peer=peer, backend=self.backend, state=self.state)
                continue
            client.peer = peer
            client._secret = self.config.secret_for_peer(peer)

    def _start_capture(self, *, suppress: bool) -> None:
        callbacks = CaptureCallbacks(
            on_move=lambda x, y: self._enqueue("move", x, y),
            on_click=lambda x, y, button, pressed: self._enqueue("click", x, y, button, pressed),
            on_scroll=lambda x, y, dx, dy: self._enqueue("scroll", x, y, dx, dy),
            on_key_press=lambda key: self._enqueue("key_press", key),
            on_key_release=lambda key: self._enqueue("key_release", key),
        )
        self.backend.start_capture(callbacks, suppress=suppress)

    def _enqueue(self, event_type: str, *values: Any) -> None:
        if self.loop is None:
            return
        self.loop.call_soon_threadsafe(self.queue.put_nowait, (event_type, values))

    async def _event_pump(self) -> None:
        while True:
            event_type, values = await self.queue.get()
            try:
                if event_type == "move":
                    await self._handle_move(int(values[0]), int(values[1]))
                elif event_type == "click":
                    await self._handle_click(int(values[0]), int(values[1]), str(values[2]), bool(values[3]))
                elif event_type == "scroll":
                    await self._handle_scroll(int(values[2]), int(values[3]))
                elif event_type == "key_press":
                    if await self._handle_host_lock_hotkey(values[0], pressed=True):
                        continue
                    await self._send_key("key_press", values[0])
                elif event_type == "key_release":
                    if await self._handle_host_lock_hotkey(values[0], pressed=False):
                        continue
                    await self._send_key("key_release", values[0])
            except Exception as exc:
                logger.exception("controller event failed")
                await self._recover_local(f"controller event failed: {exc}")

    async def _reconnect_loop(self) -> None:
        while True:
            await asyncio.sleep(5)
            for name, client in list(self.clients.items()):
                if client.connected:
                    continue
                try:
                    await client.connect()
                except Exception as exc:
                    self.state.update_peer(name, connected=False, error=str(exc))

    async def _active_watch_loop(self) -> None:
        while True:
            await asyncio.sleep(0.25)
            active = self.active
            if active is None:
                continue
            connected = bool(getattr(active.client, "connected", False))
            if not connected:
                await self._recover_local(f"{active.peer.name} disconnected")

    async def _recover_local(self, reason: str) -> None:
        await self._return_local(reason, mark_disconnected=True)

    async def _return_local(self, reason: str, *, mark_disconnected: bool) -> None:
        active = self.active
        if active is None:
            self.state.update(active_peer=None)
            if mark_disconnected:
                self.state.set_error(reason)
            return

        self.active = None
        self.state.update(active_peer=None)
        if mark_disconnected:
            self.state.update_peer(active.peer.name, connected=False, error=reason)
            self.state.set_error(f"{active.peer.name}: {reason}")
        if self.config.suppress_local_events_when_remote:
            self._start_capture(suppress=False)
        local_point = self._local_return_point(active)
        self.backend.move_to(local_point.x, local_point.y)
        self._ignore_next_lock_motion = False
        self._ignore_lock_motion_until = 0.0
        logger.info("returned control to local host after remote failure: %s", reason)

    async def _handle_host_lock_hotkey(self, key: dict[str, str], *, pressed: bool) -> bool:
        if not _is_host_lock_hotkey(key):
            return False
        if pressed:
            await self._toggle_edge_switching_pause()
        return True

    async def _toggle_edge_switching_pause(self) -> None:
        self.edge_switching_paused = not self.edge_switching_paused
        self.state.update(edge_switching_paused=self.edge_switching_paused)
        if self.edge_switching_paused:
            await self._return_local("host lock hotkey pressed", mark_disconnected=False)
        else:
            self.state.set_error(None)

    async def _handle_move(self, x: int, y: int) -> None:
        if self.active is None:
            await self._maybe_activate(Point(x, y))
            return

        if self._ignore_next_lock_motion:
            if self._is_lock_point(Point(x, y)):
                self._ignore_next_lock_motion = False
                self._ignore_lock_motion_until = 0.0
                return
            if time.monotonic() <= self._ignore_lock_motion_until:
                return
            self._ignore_next_lock_motion = False
            self._ignore_lock_motion_until = 0.0

        delta = Point(x - self.lock_point.x, y - self.lock_point.y)
        if delta.x == 0 and delta.y == 0:
            return
        if self._is_oversized_lock_delta(delta):
            self._lock_local_pointer()
            return

        active = self.active
        remote_size = Size(active.client.remote_screen.width, active.client.remote_screen.height)
        next_point = Point(
            clamp(active.point.x + delta.x, 0, remote_size.width - 1),
            clamp(active.point.y + delta.y, 0, remote_size.height - 1),
        )

        if should_exit(active.peer.edge, next_point, delta, remote_size):
            await self._deactivate(next_point)
            return

        active.point = next_point
        await active.client.send("input", {"action": "move", "x": next_point.x, "y": next_point.y})
        self.state.increment("events_forwarded")
        self._lock_local_pointer()

    async def _maybe_activate(self, point: Point) -> None:
        if self.edge_switching_paused:
            return

        for peer in self.config.peers:
            if not should_activate(peer.edge, point, self.local_size, self.config.edge_threshold_px):
                continue

            try:
                client = await self._resolve_client(peer)
            except Exception as exc:
                self.state.update_peer(peer.name, connected=False, error=str(exc))
                self.state.set_error(f"{peer.name}: {exc}")
                return

            remote_size = Size(client.remote_screen.width, client.remote_screen.height)
            remote_point = entry_position(peer.edge, point, self.local_size, remote_size)
            self.active = ActiveRemote(peer=peer, client=client, point=remote_point)
            self.state.update(active_peer=peer.name)
            await client.send("control", {"active": True})
            await client.send("input", {"action": "move", "x": remote_point.x, "y": remote_point.y})
            self.state.increment("events_forwarded")
            self._lock_local_pointer()
            if self.config.suppress_local_events_when_remote:
                self._start_capture(suppress=True)
            return

    def _lock_local_pointer(self) -> None:
        self.backend.move_to(self.lock_point.x, self.lock_point.y)
        self._ignore_next_lock_motion = True
        self._ignore_lock_motion_until = time.monotonic() + LOCK_MOTION_DROP_SECONDS

    def _is_lock_point(self, point: Point) -> bool:
        return abs(point.x - self.lock_point.x) <= 1 and abs(point.y - self.lock_point.y) <= 1

    def _is_oversized_lock_delta(self, delta: Point) -> bool:
        max_dx = max(80, round(self.local_size.width * MAX_LOCK_DELTA_RATIO))
        max_dy = max(80, round(self.local_size.height * MAX_LOCK_DELTA_RATIO))
        return abs(delta.x) > max_dx or abs(delta.y) > max_dy

    def _local_return_point(self, active: ActiveRemote) -> Point:
        try:
            return local_exit_position(
                active.peer.edge,
                active.point,
                self.local_size,
                Size(active.client.remote_screen.width, active.client.remote_screen.height),
            )
        except Exception:
            return self.lock_point

    async def _resolve_client(self, peer: PeerConfig) -> object:
        if self.host_registry is not None:
            hosted_client = await self.host_registry.get(peer.name)
            if hosted_client is not None:
                await hosted_client.connect()
                return hosted_client

        direct_client = self.clients.get(peer.name)
        if direct_client is None:
            raise ConnectionError(f"waiting for {peer.name} to connect to this host")
        await direct_client.connect()
        return direct_client

    async def _deactivate(self, remote_point: Point) -> None:
        active = self.active
        if active is None:
            return
        self.active = None
        await active.client.send("control", {"active": False})
        local_point = local_exit_position(
            active.peer.edge,
            remote_point,
            self.local_size,
            Size(active.client.remote_screen.width, active.client.remote_screen.height),
        )
        if self.config.suppress_local_events_when_remote:
            self._start_capture(suppress=False)
        self.backend.move_to(local_point.x, local_point.y)
        self.state.update(active_peer=None)

    async def _handle_click(self, _x: int, _y: int, button: str, pressed: bool) -> None:
        if self.active is None:
            return
        await self.active.client.send("input", {"action": "click", "button": button, "pressed": pressed})
        self.state.increment("events_forwarded")

    async def _handle_scroll(self, dx: int, dy: int) -> None:
        if self.active is None:
            return
        await self.active.client.send("input", {"action": "scroll", "dx": dx, "dy": dy})
        self.state.increment("events_forwarded")

    async def _send_key(self, action: str, key: dict[str, str]) -> None:
        if self.active is None:
            return
        await self.active.client.send("input", {"action": action, "key": key})
        self.state.increment("events_forwarded")


def _is_host_lock_hotkey(key: dict[str, str]) -> bool:
    return key.get("kind") == "special" and str(key.get("value", "")).lower() == "f12"
