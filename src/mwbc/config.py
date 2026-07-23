from __future__ import annotations

import json
import secrets
import socket
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_CONFIG_PATH = Path.home() / ".mwbc" / "config.json"
DEFAULT_PORT = 45445
DEFAULT_DASHBOARD_PORT = 45446
DEFAULT_SCROLL_MULTIPLIER = 1.0
MIN_SCROLL_MULTIPLIER = 1.0
MAX_SCROLL_MULTIPLIER = 8.0
VALID_EDGES = {"left", "right", "top", "bottom"}


def normalize_scroll_multiplier(value: Any) -> float:
    try:
        multiplier = float(value)
    except (TypeError, ValueError):
        multiplier = DEFAULT_SCROLL_MULTIPLIER
    return min(MAX_SCROLL_MULTIPLIER, max(MIN_SCROLL_MULTIPLIER, multiplier))


@dataclass(slots=True)
class PeerConfig:
    name: str
    host: str = ""
    edge: str = "right"
    port: int = DEFAULT_PORT
    keep_awake: bool = False
    keep_awake_interval_seconds: float = 45.0
    scroll_multiplier: float = DEFAULT_SCROLL_MULTIPLIER
    shared_secret: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PeerConfig":
        edge = str(data.get("edge", "right")).lower()
        if edge not in VALID_EDGES:
            raise ValueError(f"invalid peer edge {edge!r}; expected one of {sorted(VALID_EDGES)}")

        name = str(data.get("name") or data.get("host") or "").strip()
        host = str(data.get("host") or "").strip()
        if not name:
            raise ValueError("peer is missing a name")

        return cls(
            name=name,
            host=host,
            edge=edge,
            port=int(data.get("port", DEFAULT_PORT)),
            keep_awake=bool(data.get("keep_awake", False)),
            keep_awake_interval_seconds=float(data.get("keep_awake_interval_seconds", 45.0)),
            scroll_multiplier=normalize_scroll_multiplier(data.get("scroll_multiplier", DEFAULT_SCROLL_MULTIPLIER)),
            shared_secret=data.get("shared_secret") or None,
        )

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        if not self.host:
            data.pop("host")
        if self.shared_secret is None:
            data.pop("shared_secret")
        return data

    def client_settings(self) -> dict[str, Any]:
        return {
            "keep_awake": self.keep_awake,
            "keep_awake_interval_seconds": self.keep_awake_interval_seconds,
        }


@dataclass(slots=True)
class AppConfig:
    machine_name: str = field(default_factory=socket.gethostname)
    pairing_secret: str = field(default_factory=lambda: secrets.token_urlsafe(32))
    listen_host: str = "0.0.0.0"
    listen_port: int = DEFAULT_PORT
    dashboard_host: str = "127.0.0.1"
    dashboard_port: int = DEFAULT_DASHBOARD_PORT
    backend: str = "auto"
    clipboard_enabled: bool = True
    clipboard_poll_seconds: float = 0.5
    clipboard_max_text_bytes: int = 262_144
    suppress_local_events_when_remote: bool = True
    edge_threshold_px: int = 2
    peers: list[PeerConfig] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AppConfig":
        peers = [PeerConfig.from_dict(item) for item in data.get("peers", [])]
        pairing_secret = str(data.get("pairing_secret") or "").strip()
        return cls(
            machine_name=str(data.get("machine_name") or socket.gethostname()),
            pairing_secret=pairing_secret or secrets.token_urlsafe(32),
            listen_host=str(data.get("listen_host", "0.0.0.0")),
            listen_port=int(data.get("listen_port", DEFAULT_PORT)),
            dashboard_host=str(data.get("dashboard_host", "127.0.0.1")),
            dashboard_port=int(data.get("dashboard_port", DEFAULT_DASHBOARD_PORT)),
            backend=str(data.get("backend", "auto")),
            clipboard_enabled=bool(data.get("clipboard_enabled", True)),
            clipboard_poll_seconds=float(data.get("clipboard_poll_seconds", 0.5)),
            clipboard_max_text_bytes=int(data.get("clipboard_max_text_bytes", 262_144)),
            suppress_local_events_when_remote=bool(data.get("suppress_local_events_when_remote", True)),
            edge_threshold_px=int(data.get("edge_threshold_px", 2)),
            peers=peers,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "machine_name": self.machine_name,
            "pairing_secret": self.pairing_secret,
            "listen_host": self.listen_host,
            "listen_port": self.listen_port,
            "dashboard_host": self.dashboard_host,
            "dashboard_port": self.dashboard_port,
            "backend": self.backend,
            "clipboard_enabled": self.clipboard_enabled,
            "clipboard_poll_seconds": self.clipboard_poll_seconds,
            "clipboard_max_text_bytes": self.clipboard_max_text_bytes,
            "suppress_local_events_when_remote": self.suppress_local_events_when_remote,
            "edge_threshold_px": self.edge_threshold_px,
            "peers": [peer.to_dict() for peer in self.peers],
        }

    def secret_for_peer(self, peer: PeerConfig) -> str:
        return peer.shared_secret or self.pairing_secret

    def accepted_secrets(self) -> list[str]:
        values = [self.pairing_secret]
        values.extend(peer.shared_secret for peer in self.peers if peer.shared_secret)
        return list(dict.fromkeys(values))


def load_config(path: Path = DEFAULT_CONFIG_PATH) -> AppConfig:
    with path.expanduser().open("r", encoding="utf-8") as handle:
        return AppConfig.from_dict(json.load(handle))


def save_config(config: AppConfig, path: Path = DEFAULT_CONFIG_PATH) -> None:
    target = path.expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(config.to_dict(), indent=2) + "\n", encoding="utf-8")


def ensure_config(path: Path = DEFAULT_CONFIG_PATH) -> AppConfig:
    target = path.expanduser()
    if target.exists():
        return load_config(target)
    config = AppConfig()
    save_config(config, target)
    return config


def generate_secret() -> str:
    return secrets.token_urlsafe(32)
