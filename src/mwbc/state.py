from __future__ import annotations

import time
from dataclasses import dataclass, field
from threading import Lock
from typing import Any


@dataclass(slots=True)
class PeerStatus:
    name: str
    device_id: str | None
    host: str
    port: int
    edge: str
    connected: bool = False
    health: str = "offline"
    screen_width: int | None = None
    screen_height: int | None = None
    last_seen: float | None = None
    error: str | None = None
    capabilities: dict[str, Any] = field(default_factory=dict)

    def to_dict(self, *, now: float | None = None) -> dict[str, Any]:
        last_seen_age = None
        if self.last_seen is not None:
            last_seen_age = round((now or time.time()) - self.last_seen, 1)
        return {
            "name": self.name,
            "device_id": self.device_id,
            "host": self.host,
            "port": self.port,
            "edge": self.edge,
            "connected": self.connected,
            "health": self.health,
            "screen_width": self.screen_width,
            "screen_height": self.screen_height,
            "last_seen": self.last_seen,
            "last_seen_age": last_seen_age,
            "error": self.error,
            "capabilities": self.capabilities,
        }


class StateStore:
    def __init__(self, machine_name: str, backend_name: str, device_id: str | None = None) -> None:
        self._lock = Lock()
        self._data: dict[str, Any] = {
            "machine_name": machine_name,
            "device_id": device_id,
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

    def register_peer(self, name: str, host: str, port: int, edge: str, device_id: str | None = None) -> None:
        with self._lock:
            peers = self._data["peers"]
            peer = peers.get(name)
            if peer is None:
                peers[name] = PeerStatus(name=name, device_id=device_id, host=host, port=port, edge=edge)
                return
            if device_id is not None:
                peer.device_id = device_id
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
            now = time.time()
            data = dict(self._data)
            data["peers"] = [peer.to_dict(now=now) for peer in self._data["peers"].values()]
            incoming_clients = {}
            for key, value in self._data["incoming_clients"].items():
                entry = dict(value)
                last_seen = entry.get("last_seen")
                if last_seen is not None:
                    try:
                        entry["last_seen_age"] = round(now - float(last_seen), 1)
                    except (TypeError, ValueError):
                        entry["last_seen_age"] = None
                entry.setdefault("health", "connected" if entry.get("connected", True) else "offline")
                incoming_clients[key] = entry
            data["incoming_clients"] = incoming_clients
            data["uptime_seconds"] = round(now - float(self._data["started_at"]), 1)
            return data
