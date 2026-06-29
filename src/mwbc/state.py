from __future__ import annotations

import time
from dataclasses import dataclass, field
from threading import Lock
from typing import Any


@dataclass(slots=True)
class PeerStatus:
    name: str
    host: str
    port: int
    edge: str
    connected: bool = False
    screen_width: int | None = None
    screen_height: int | None = None
    last_seen: float | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "host": self.host,
            "port": self.port,
            "edge": self.edge,
            "connected": self.connected,
            "screen_width": self.screen_width,
            "screen_height": self.screen_height,
            "last_seen": self.last_seen,
            "error": self.error,
        }


class StateStore:
    def __init__(self, machine_name: str, backend_name: str) -> None:
        self._lock = Lock()
        self._data: dict[str, Any] = {
            "machine_name": machine_name,
            "backend": backend_name,
            "mode": "starting",
            "started_at": time.time(),
            "active_peer": None,
            "last_error": None,
            "local_screen_width": None,
            "local_screen_height": None,
            "events_forwarded": 0,
            "events_received": 0,
            "peers": {},
            "incoming_clients": {},
        }

    def update(self, **values: Any) -> None:
        with self._lock:
            self._data.update(values)

    def increment(self, key: str, amount: int = 1) -> None:
        with self._lock:
            self._data[key] = int(self._data.get(key, 0)) + amount

    def set_error(self, message: str | None) -> None:
        self.update(last_error=message)

    def register_peer(self, name: str, host: str, port: int, edge: str) -> None:
        with self._lock:
            peers = self._data["peers"]
            peer = peers.get(name)
            if peer is None:
                peers[name] = PeerStatus(name=name, host=host, port=port, edge=edge)
                return
            peer.host = host
            peer.port = port
            peer.edge = edge

    def update_peer(self, name: str, **values: Any) -> None:
        with self._lock:
            peer = self._data["peers"].get(name)
            if peer is None:
                return
            for key, value in values.items():
                setattr(peer, key, value)

    def update_incoming(self, key: str, **values: Any) -> None:
        with self._lock:
            incoming = self._data["incoming_clients"]
            current = incoming.setdefault(key, {})
            current.update(values)

    def remove_incoming(self, key: str) -> None:
        with self._lock:
            self._data["incoming_clients"].pop(key, None)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            data = dict(self._data)
            data["peers"] = [peer.to_dict() for peer in self._data["peers"].values()]
            data["incoming_clients"] = dict(self._data["incoming_clients"])
            data["uptime_seconds"] = round(time.time() - float(self._data["started_at"]), 1)
            return data
