from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import urllib.error
import urllib.request
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import AppConfig, load_config


DEFAULT_API_URL = "http://127.0.0.1:45446"
DEFAULT_SMITH_URL = "http://localhost:3000/mwbc"


@dataclass(frozen=True, slots=True)
class TraySnapshot:
    online: bool
    status_text: str
    detail_text: str
    mode: str = "offline"
    active_peer: str | None = None
    host_lock: bool = False
    connected_peers: tuple[str, ...] = ()
    error: str | None = None


def normalize_base_url(url: str) -> str:
    return str(url or DEFAULT_API_URL).strip().rstrip("/")


def api_path(base_url: str, path: str) -> str:
    clean_path = "/" + path.lstrip("/")
    return f"{normalize_base_url(base_url)}{clean_path}"


def dashboard_url(base_url: str) -> str:
    return normalize_base_url(base_url)


def layout_url(base_url: str, token: str) -> str:
    return f"{api_path(base_url, '/layout')}#token={token}"


def controller_url(base_url: str, token: str) -> str:
    return f"{api_path(base_url, '/controller')}#token={token}"


def summarize_status(status: dict[str, Any] | None, service: dict[str, Any] | None, error: str | None = None) -> TraySnapshot:
    if error is not None:
        return TraySnapshot(
            online=False,
            status_text="MWBC offline",
            detail_text=error,
            error=error,
        )

    if not isinstance(status, dict):
        return TraySnapshot(
            online=False,
            status_text="MWBC offline",
            detail_text="Dashboard API is not responding",
            error="Dashboard API is not responding",
        )

    peers = tuple(_connected_peer_names(status))
    mode = str(status.get("mode") or (service or {}).get("mode") or "running")
    active_peer = status.get("active_peer")
    active_peer = str(active_peer) if active_peer else None
    host_lock = bool(status.get("edge_switching_paused"))
    machine_name = str(status.get("machine_name") or (service or {}).get("machine_name") or "MWBC")

    if host_lock:
        status_text = "MWBC locked to host"
    elif active_peer:
        status_text = f"MWBC controlling {active_peer}"
    elif peers:
        status_text = f"MWBC connected: {', '.join(peers)}"
    else:
        status_text = "MWBC running"

    detail_parts = [machine_name, f"mode: {mode}"]
    if peers:
        detail_parts.append(f"devices: {', '.join(peers)}")
    if host_lock:
        hotkey = status.get("host_lock_hotkey") or "F12"
        detail_parts.append(f"{hotkey} lock on")

    return TraySnapshot(
        online=True,
        status_text=status_text,
        detail_text=" | ".join(detail_parts),
        mode=mode,
        active_peer=active_peer,
        host_lock=host_lock,
        connected_peers=peers,
    )


def build_start_argv(
    *,
    config_path: Path,
    log_level: str = "INFO",
    mode: str = "host",
    backend: str | None = None,
    dashboard_host: str | None = None,
    dashboard_port: int | None = None,
) -> list[str]:
    argv = [_python_or_frozen_executable()]
    if not getattr(sys, "frozen", False):
        argv.extend(["-m", "mwbc"])
    argv.extend(["--config", str(config_path.expanduser()), "--log-level", str(log_level), mode])
    if backend:
        argv.extend(["--backend", backend])
    if mode in {"run", "host"}:
        if dashboard_host:
            argv.extend(["--dashboard-host", dashboard_host])
        if dashboard_port is not None:
            argv.extend(["--dashboard-port", str(dashboard_port)])
    return argv


def cmd_tray(args: Any) -> int:
    try:
        import pystray
        from PIL import Image, ImageDraw
    except ImportError:
        print('Tray support requires the optional tray dependencies. Run: pip install -e ".[input,tray]"')
        return 1

    config = load_config(args.config)
    app = TrayApp(
        args=args,
        config=config,
        pystray_module=pystray,
        image_module=Image,
        image_draw_module=ImageDraw,
    )
    app.run()
    return 0


class TrayApp:
    def __init__(
        self,
        *,
        args: Any,
        config: AppConfig,
        pystray_module: Any,
        image_module: Any,
        image_draw_module: Any,
    ) -> None:
        self.args = args
        self.config = config
        self.pystray = pystray_module
        self.Image = image_module
        self.ImageDraw = image_draw_module
        self.api_url = normalize_base_url(args.api_url)
        self.smith_url = str(args.smith_url or DEFAULT_SMITH_URL)
        self.token = str(args.token or config.pairing_secret)
        self.poll_seconds = max(0.5, float(args.poll_seconds))
        self.start_mode = str(args.start_mode)
        self.start_backend = args.start_backend or config.backend
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._snapshot = TraySnapshot(
            online=False,
            status_text="MWBC starting tray",
            detail_text="Checking local service",
        )
        self.icon: Any | None = None

    def run(self) -> None:
        self._refresh()
        self.icon = self.pystray.Icon("MWBC", self._make_icon(self._snapshot), self._title(self._snapshot))
        self.icon.menu = self._build_menu(self._snapshot)
        poller = threading.Thread(target=self._poll_loop, name="mwbc-tray-poller", daemon=True)
        poller.start()
        self.icon.run()

    def _poll_loop(self) -> None:
        while not self._stop_event.wait(self.poll_seconds):
            self._refresh()

    def _refresh(self) -> None:
        status: dict[str, Any] | None = None
        service: dict[str, Any] | None = None
        error: str | None = None
        try:
            status = self._get_json("/api/status")
            with suppress_http_errors():
                service = self._get_json("/api/service")
        except Exception as exc:
            error = _short_error(exc)

        snapshot = summarize_status(status, service, error)
        with self._lock:
            self._snapshot = snapshot

        icon = self.icon
        if icon is not None:
            icon.title = self._title(snapshot)
            icon.icon = self._make_icon(snapshot)
            icon.menu = self._build_menu(snapshot)
            icon.update_menu()

    def _build_menu(self, snapshot: TraySnapshot) -> Any:
        menu = self.pystray.Menu
        item = self.pystray.MenuItem
        rows = [
            item(snapshot.status_text, _noop, enabled=False),
            item(snapshot.detail_text, _noop, enabled=False),
            menu.SEPARATOR,
            item("Open Smith MWBC", self._open_smith),
            item("Open MWBC Dashboard", self._open_dashboard, enabled=snapshot.online),
            item("Open Layout Editor", self._open_layout, enabled=snapshot.online),
            item("Open Web Controller", self._open_controller, enabled=snapshot.online),
            menu.SEPARATOR,
        ]
        if snapshot.online:
            rows.extend(
                [
                    item("Restart Host", self._restart_host),
                    item("Stop MWBC", self._stop_mwbc),
                ]
            )
        else:
            rows.append(item("Start Host", self._start_host))
        rows.extend([menu.SEPARATOR, item("Quit Tray", self._quit)])
        return menu(*rows)

    def _make_icon(self, snapshot: TraySnapshot) -> Any:
        image = self.Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        draw = self.ImageDraw.Draw(image)
        if not snapshot.online:
            fill = (215, 58, 73, 255)
        elif snapshot.host_lock:
            fill = (50, 115, 220, 255)
        elif snapshot.connected_peers:
            fill = (38, 166, 91, 255)
        else:
            fill = (230, 165, 35, 255)
        draw.rounded_rectangle((6, 6, 58, 58), radius=12, fill=fill)
        foreground = (255, 255, 255, 245)
        shadow = (20, 24, 32, 80)
        screen = tuple(max(0, channel - 36) for channel in fill[:3]) + (255,)

        draw.rounded_rectangle((11, 14, 43, 37), radius=4, fill=foreground)
        draw.rounded_rectangle((15, 18, 39, 32), radius=2, fill=screen)
        draw.rectangle((25, 38, 30, 45), fill=foreground)
        draw.rounded_rectangle((19, 46, 36, 51), radius=2, fill=foreground)

        draw.line((39, 30, 47, 31, 50, 35), fill=foreground, width=2)
        draw.rounded_rectangle((43, 34, 56, 52), radius=7, fill=foreground)
        draw.line((49, 36, 49, 43), fill=screen, width=1)
        draw.line((43, 43, 56, 43), fill=shadow, width=1)
        return image

    def _title(self, snapshot: TraySnapshot) -> str:
        title = snapshot.status_text
        if snapshot.host_lock:
            title = f"{title} (F12 lock)"
        return title[:63]

    def _open_smith(self, _icon: Any = None, _item: Any = None) -> None:
        self._open_url(self.smith_url)

    def _open_dashboard(self, _icon: Any = None, _item: Any = None) -> None:
        self._open_url(dashboard_url(self.api_url))

    def _open_layout(self, _icon: Any = None, _item: Any = None) -> None:
        self._open_url(layout_url(self.api_url, self.token))

    def _open_controller(self, _icon: Any = None, _item: Any = None) -> None:
        self._open_url(controller_url(self.api_url, self.token))

    def _start_host(self, _icon: Any = None, _item: Any = None) -> None:
        argv = build_start_argv(
            config_path=self.args.config,
            log_level=self.args.log_level,
            mode=self.start_mode,
            backend=self.start_backend,
            dashboard_host=self.config.dashboard_host,
            dashboard_port=self.config.dashboard_port,
        )
        _spawn_detached(argv)
        self._refresh()

    def _stop_mwbc(self, _icon: Any = None, _item: Any = None) -> None:
        self._post_json("/api/service/stop", {})
        self._refresh()

    def _restart_host(self, _icon: Any = None, _item: Any = None) -> None:
        self._post_json(
            "/api/service/restart",
            {
                "mode": self.start_mode,
                "backend": self.start_backend,
                "dashboard_host": self.config.dashboard_host,
                "dashboard_port": self.config.dashboard_port,
                "log_level": self.args.log_level,
            },
        )
        self._refresh()

    def _quit(self, icon: Any = None, _item: Any = None) -> None:
        self._stop_event.set()
        target = icon or self.icon
        if target is not None:
            target.stop()

    def _get_json(self, path: str) -> dict[str, Any]:
        request = urllib.request.Request(api_path(self.api_url, path), method="GET")
        return _read_json(request)

    def _post_json(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        body = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            api_path(self.api_url, path),
            data=body,
            headers={"Content-Type": "application/json", "X-MWBC-Token": self.token},
            method="POST",
        )
        return _read_json(request)

    def _open_url(self, url: str) -> None:
        webbrowser.open(url)


class suppress_http_errors:
    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type: Any, _exc: Any, _traceback: Any) -> bool:
        return bool(
            exc_type is not None
            and issubclass(exc_type, (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError))
        )


def _connected_peer_names(status: dict[str, Any]) -> list[str]:
    names: list[str] = []
    for peer in status.get("peers") or []:
        if isinstance(peer, dict) and peer.get("connected"):
            name = str(peer.get("name") or "").strip()
            if name:
                names.append(name)
    for incoming in (status.get("incoming_clients") or {}).values():
        if not isinstance(incoming, dict) or not incoming.get("connected", True):
            continue
        name = str(incoming.get("machine_name") or incoming.get("peer") or "").strip()
        if name:
            names.append(name)
    return list(dict.fromkeys(names))


def _read_json(request: urllib.request.Request) -> dict[str, Any]:
    with urllib.request.urlopen(request, timeout=2.5) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if isinstance(payload, dict) and isinstance(payload.get("state"), dict):
        return payload["state"]
    return payload if isinstance(payload, dict) else {}


def _short_error(exc: Exception) -> str:
    if isinstance(exc, urllib.error.HTTPError):
        return f"HTTP {exc.code}"
    if isinstance(exc, urllib.error.URLError):
        return str(exc.reason)
    return str(exc)


def _python_or_frozen_executable() -> str:
    return sys.executable


def _spawn_detached(argv: list[str]) -> None:
    kwargs: dict[str, Any] = {"cwd": os.getcwd()}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen(argv, **kwargs)


def _noop(*_args: Any, **_kwargs: Any) -> None:
    return None
