from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import logging
import signal
import socket
import subprocess
import sys
import threading
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .config import (
    VALID_EDGES,
    AppConfig,
    DEFAULT_CONFIG_PATH,
    PeerConfig,
    ensure_config,
    generate_secret,
    load_config,
    save_config,
)
from .clipboard import create_clipboard
from .controller import BorderController
from .dashboard import DashboardServer
from .input_backend import apply_input_event, create_backend
from .logging_utils import install_recent_log_handler, recent_logs
from .network import AgentServer, ClientConnector, HostClientRegistry
from .startup import StartupError, StartupOptions, install_startup, startup_status, uninstall_startup
from .state import StateStore


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mwbc", description="LAN keyboard and mouse sharing daemon.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH, help="Path to config JSON.")
    parser.add_argument("--log-level", default="INFO", help="Python logging level.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init = subparsers.add_parser("init", help="Create a default config file.")
    init.add_argument("--force", action="store_true", help="Overwrite an existing config.")
    init.add_argument("--name", help="Machine name to advertise.")

    subparsers.add_parser("secret", help="Print a new pairing secret.")

    peer = subparsers.add_parser("add-peer", help="Add or replace a configured peer.")
    peer.add_argument("--name", required=True)
    peer.add_argument("--host", default="", help="Peer IP/hostname. Omit when the peer will connect outbound.")
    peer.add_argument("--edge", choices=["left", "right", "top", "bottom"], default="right")
    peer.add_argument("--port", type=int, default=45445)
    peer.add_argument("--shared-secret", help="Optional peer-specific secret.")

    run = subparsers.add_parser("run", help="Run agent, controller, and dashboard.")
    run.add_argument("--no-dashboard", action="store_true")
    run.add_argument("--backend", choices=["auto", "pynput", "null"], help="Override configured backend.")
    run.add_argument("--dashboard-host", help="Override dashboard bind host.")
    run.add_argument("--dashboard-port", type=int, help="Override dashboard port.")

    agent = subparsers.add_parser("agent", help="Run the receiving input agent only.")
    agent.add_argument("--backend", choices=["auto", "pynput", "null"], help="Override configured backend.")

    controller = subparsers.add_parser("controller", help="Run the border controller only.")
    controller.add_argument("--backend", choices=["auto", "pynput", "null"], help="Override configured backend.")

    host = subparsers.add_parser("host", help="Run as the host for always-looking clients.")
    host.add_argument("--no-dashboard", action="store_true")
    host.add_argument("--backend", choices=["auto", "pynput", "null"], help="Override configured backend.")
    host.add_argument("--dashboard-host", help="Override dashboard bind host.")
    host.add_argument("--dashboard-port", type=int, help="Override dashboard port.")

    client = subparsers.add_parser("client", help="Keep connecting to a host and receive remote input.")
    client.add_argument("--host", required=True, help="Host IP address or DNS name.")
    client.add_argument("--port", type=int, default=45445, help="Host TCP port.")
    client.add_argument("--retry-seconds", type=float, default=1.0, help="Reconnect delay.")
    client.add_argument("--backend", choices=["auto", "pynput", "null"], help="Override configured backend.")

    startup = subparsers.add_parser("startup", help="Manage per-user startup registration.")
    startup_subparsers = startup.add_subparsers(dest="startup_command", required=True)

    startup_install = startup_subparsers.add_parser("install", help="Launch MWBC automatically when you sign in.")
    startup_install.add_argument("--mode", choices=["run", "agent", "controller", "host", "client"], default="run")
    startup_install.add_argument("--backend", choices=["auto", "pynput", "null"], help="Override configured backend.")
    startup_install.add_argument("--host", help="Host IP/hostname when mode is client.")
    startup_install.add_argument("--port", type=int, default=45445, help="Host TCP port when mode is client.")
    startup_install.add_argument("--retry-seconds", type=float, default=1.0, help="Reconnect delay when mode is client.")
    startup_install.add_argument("--no-dashboard", action="store_true", help="Disable dashboard when mode is run or host.")
    startup_install.add_argument("--dashboard-host", help="Dashboard bind host when mode is run or host.")
    startup_install.add_argument("--dashboard-port", type=int, help="Dashboard port when mode is run or host.")
    startup_install.add_argument("--keep-alive", action="store_true", help="On macOS, restart after unexpected exit.")

    startup_subparsers.add_parser("uninstall", help="Remove MWBC from startup.")
    startup_subparsers.add_parser("status", help="Show current startup registration.")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    install_recent_log_handler()

    if args.command == "init":
        return cmd_init(args)
    if args.command == "secret":
        print(generate_secret())
        return 0
    if args.command == "add-peer":
        return cmd_add_peer(args)
    if args.command == "startup":
        return cmd_startup(args)
    if args.command in {"run", "agent", "controller", "host", "client"}:
        return asyncio.run(cmd_run(args))

    parser.error("unknown command")
    return 2


def cmd_init(args: argparse.Namespace) -> int:
    path = args.config.expanduser()
    if path.exists() and not args.force:
        print(f"Config already exists: {path}")
        return 1
    config = AppConfig()
    if args.name:
        config.machine_name = args.name
    save_config(config, path)
    print(f"Created config: {path}")
    print(f"Pairing secret: {config.pairing_secret}")
    return 0


def cmd_add_peer(args: argparse.Namespace) -> int:
    config = ensure_config(args.config)
    replacement = PeerConfig(
        name=args.name,
        host=args.host,
        edge=args.edge,
        port=args.port,
        shared_secret=args.shared_secret,
    )
    config.peers = [peer for peer in config.peers if peer.name != replacement.name]
    config.peers.append(replacement)
    save_config(config, args.config)
    print(f"Saved peer {replacement.name!r} in {args.config.expanduser()}")
    return 0


def cmd_startup(args: argparse.Namespace) -> int:
    try:
        if args.startup_command == "install":
            status = install_startup(
                StartupOptions(
                    mode=args.mode,
                    config_path=args.config,
                    host=args.host,
                    port=args.port,
                    retry_seconds=args.retry_seconds,
                    no_dashboard=args.no_dashboard,
                    dashboard_host=args.dashboard_host,
                    dashboard_port=args.dashboard_port,
                    backend=args.backend,
                    log_level=args.log_level,
                    keep_alive=args.keep_alive,
                )
            )
            _print_startup_status(status, verb="Installed")
            return 0
        if args.startup_command == "uninstall":
            status = uninstall_startup()
            _print_startup_status(status, verb="Removed")
            return 0
        if args.startup_command == "status":
            status = startup_status()
            _print_startup_status(status, verb="Status")
            return 0 if status.supported else 1
    except StartupError as exc:
        print(f"Startup error: {exc}")
        return 1
    print("Unknown startup command")
    return 2


def _print_startup_status(status, *, verb: str) -> None:
    print(f"{verb}: {'yes' if status.installed else 'no'}")
    print(f"Platform: {status.platform_name}")
    if status.location:
        print(f"Location: {status.location}")
    if status.command:
        print(f"Command: {status.command}")
    if status.message:
        print(f"Message: {status.message}")


def _build_layout_provider(config: AppConfig, state: StateStore, layout_lock: threading.RLock):
    def provide() -> dict[str, Any]:
        with layout_lock:
            return _layout_snapshot(config, state)

    return provide


def _build_layout_update_handler(
    config: AppConfig,
    config_path: Path,
    state: StateStore,
    controller_ref: dict[str, BorderController | None],
    host_registry: HostClientRegistry | None,
    loop: asyncio.AbstractEventLoop,
    layout_lock: threading.RLock,
):
    def handle(payload: dict[str, Any]) -> dict[str, Any]:
        requested = payload.get("peers")
        if not isinstance(requested, list) or not all(isinstance(item, dict) for item in requested):
            raise ValueError("peers must be a list of objects")

        updates_by_name: dict[str, dict[str, Any]] = {}
        for item in requested:
            name = str(item.get("name") or "").strip()
            edge = str(item.get("edge") or "").lower().strip()
            if not name:
                raise ValueError("peer is missing a name")
            if edge not in VALID_EDGES:
                raise ValueError(f"invalid edge {edge!r}; expected one of {sorted(VALID_EDGES)}")
            interval = float(item.get("keep_awake_interval_seconds", 45.0))
            if interval < 5 or interval > 3600:
                raise ValueError("keep-awake interval must be between 5 and 3600 seconds")
            updates_by_name[name] = {
                "edge": edge,
                "keep_awake": bool(item.get("keep_awake", False)),
                "keep_awake_interval_seconds": interval,
            }

        with layout_lock:
            known = {peer.name: peer for peer in config.peers}
            unknown = sorted(name for name in updates_by_name if name not in known)
            if unknown:
                raise ValueError(f"unknown peer: {', '.join(unknown)}")

            for name, updates in updates_by_name.items():
                peer = known[name]
                peer.edge = updates["edge"]
                peer.keep_awake = updates["keep_awake"]
                peer.keep_awake_interval_seconds = updates["keep_awake_interval_seconds"]
            save_config(config, config_path)
            for peer in config.peers:
                state.register_peer(peer.name, peer.host, peer.port, peer.edge)

        controller = controller_ref.get("controller")
        if controller is not None and controller.loop is not None and controller.loop.is_running():
            future = asyncio.run_coroutine_threadsafe(controller.refresh_peers(), controller.loop)
            future.result(timeout=2)

        if host_registry is not None and updates_by_name:
            future = asyncio.run_coroutine_threadsafe(
                _send_peer_settings(config, host_registry, updates_by_name.keys()),
                loop,
            )
            future.result(timeout=2)

        with layout_lock:
            return _layout_snapshot(config, state)

    return handle


def _layout_snapshot(config: AppConfig, state: StateStore) -> dict[str, Any]:
    current = {peer["name"]: peer for peer in state.snapshot().get("peers", [])}
    peers: list[dict[str, Any]] = []
    for peer in config.peers:
        status = current.get(peer.name, {})
        peers.append(
            {
                "name": peer.name,
                "host": peer.host,
                "port": peer.port,
                "edge": peer.edge,
                "connected": bool(status.get("connected")),
                "screen_width": status.get("screen_width"),
                "screen_height": status.get("screen_height"),
                "error": status.get("error"),
                "keep_awake": peer.keep_awake,
                "keep_awake_interval_seconds": peer.keep_awake_interval_seconds,
            }
        )
    return {"machine_name": config.machine_name, "peers": peers}


async def _send_peer_settings(config: AppConfig, host_registry: HostClientRegistry, names: Any) -> None:
    known = {peer.name: peer for peer in config.peers}
    for name in names:
        peer = known.get(str(name))
        if peer is None:
            continue
        client = await host_registry.get(peer.name)
        if client is None:
            continue
        try:
            await client.send("settings", peer.client_settings())
        except Exception as exc:
            logging.getLogger(__name__).info("failed to send settings to %s: %s", peer.name, exc)


def _build_management_handler(
    config: AppConfig,
    config_path: Path,
    args: argparse.Namespace,
    state: StateStore,
    loop: asyncio.AbstractEventLoop,
    stop_event: asyncio.Event,
    restart_request: dict[str, list[str] | None],
    auth_tokens: list[str],
):
    def handle(action: str, payload: dict[str, Any]) -> dict[str, Any]:
        if action == "capabilities":
            return _capabilities_payload()
        if action == "service.status":
            return _service_status_payload(config, args, state)
        if action == "service.stop":
            loop.call_soon_threadsafe(stop_event.set)
            return {"ok": True, "action": "stop", "message": "MWBC is stopping"}
        if action == "service.restart":
            restart_request["argv"] = _build_service_argv(config, args, payload)
            loop.call_soon_threadsafe(stop_event.set)
            return {"ok": True, "action": "restart", "message": "MWBC is restarting"}
        if action == "service.start":
            restart_request["argv"] = _build_service_argv(config, args, payload)
            loop.call_soon_threadsafe(stop_event.set)
            return {"ok": True, "action": "start", "message": "MWBC is starting the requested mode"}
        if action == "startup.status":
            return {"startup": _startup_status_payload(startup_status())}
        if action == "startup.install":
            status = install_startup(_startup_options_from_payload(config, args, payload))
            return {"startup": _startup_status_payload(status)}
        if action == "startup.uninstall":
            status = uninstall_startup()
            return {"startup": _startup_status_payload(status)}
        if action == "logs":
            limit = int(payload.get("limit", 100))
            return {"logs": recent_logs(limit)}
        if action == "secret.regenerate":
            config.pairing_secret = generate_secret()
            save_config(config, config_path)
            auth_tokens[:] = config.accepted_secrets()
            return {
                "pairing_secret": config.pairing_secret,
                "dashboard_url": _dashboard_url(config),
                "layout_url": _layout_url(config),
                "controller_url": _controller_url(config),
            }
        raise ValueError(f"unknown management action {action!r}")

    return handle


def _capabilities_payload() -> dict[str, Any]:
    return {
        "service": {
            "status": True,
            "start": True,
            "stop": True,
            "restart": True,
            "start_requires_running_api": True,
        },
        "startup": {
            "status": True,
            "install": True,
            "uninstall": True,
        },
        "config": {
            "secret_regeneration": True,
            "layout": True,
            "keep_awake": True,
        },
        "logs": True,
    }


def _service_status_payload(config: AppConfig, args: argparse.Namespace, state: StateStore) -> dict[str, Any]:
    snapshot = state.snapshot()
    return {
        "running": True,
        "pid": os.getpid(),
        "mode": snapshot.get("mode") or args.command,
        "command": args.command,
        "config_path": str(args.config.expanduser()),
        "dashboard_url": _dashboard_url(config),
        "layout_url": _layout_url(config) if args.command in {"run", "host"} else None,
        "controller_url": _controller_url(config) if args.command in {"run", "host"} else None,
        "backend": snapshot.get("backend"),
        "machine_name": snapshot.get("machine_name"),
        "uptime_seconds": snapshot.get("uptime_seconds"),
    }


def _startup_status_payload(status) -> dict[str, Any]:
    return asdict(status)


def _startup_options_from_payload(config: AppConfig, args: argparse.Namespace, payload: dict[str, Any]) -> StartupOptions:
    mode = str(payload.get("mode") or getattr(args, "command", "host"))
    if mode not in {"run", "agent", "controller", "host", "client"}:
        raise ValueError("startup mode must be run, agent, controller, host, or client")
    host = payload.get("host")
    if host is None:
        host = getattr(args, "host", None)
    return StartupOptions(
        mode=mode,
        config_path=args.config,
        host=str(host) if host else None,
        port=int(payload.get("port", getattr(args, "port", config.listen_port))),
        retry_seconds=float(payload.get("retry_seconds", getattr(args, "retry_seconds", 1.0))),
        no_dashboard=bool(payload.get("no_dashboard", getattr(args, "no_dashboard", False))),
        dashboard_host=str(payload.get("dashboard_host", config.dashboard_host)),
        dashboard_port=int(payload.get("dashboard_port", config.dashboard_port)),
        backend=payload.get("backend") or getattr(args, "backend", None),
        log_level=str(payload.get("log_level", getattr(args, "log_level", "INFO"))),
        keep_alive=bool(payload.get("keep_alive", False)),
    )


def _build_service_argv(config: AppConfig, args: argparse.Namespace, payload: dict[str, Any]) -> list[str]:
    mode = str(payload.get("mode") or args.command)
    if mode not in {"run", "agent", "controller", "host", "client"}:
        raise ValueError("mode must be run, agent, controller, host, or client")

    argv = [_python_or_frozen_executable()]
    if not getattr(sys, "frozen", False):
        argv.extend(["-m", "mwbc"])
    argv.extend(["--config", str(args.config.expanduser()), "--log-level", str(payload.get("log_level", args.log_level))])
    argv.append(mode)

    backend = payload.get("backend") or getattr(args, "backend", None)
    if backend:
        argv.extend(["--backend", str(backend)])

    if mode == "client":
        host = payload.get("host") or getattr(args, "host", None)
        if not host:
            raise ValueError("client mode requires host")
        argv.extend(["--host", str(host), "--port", str(payload.get("port", getattr(args, "port", config.listen_port)))])
        retry_seconds = payload.get("retry_seconds", getattr(args, "retry_seconds", 1.0))
        if float(retry_seconds) != 1.0:
            argv.extend(["--retry-seconds", str(retry_seconds)])

    if mode in {"run", "host"}:
        if bool(payload.get("no_dashboard", False)):
            argv.append("--no-dashboard")
        dashboard_host = str(payload.get("dashboard_host", config.dashboard_host))
        dashboard_port = int(payload.get("dashboard_port", config.dashboard_port))
        argv.extend(["--dashboard-host", dashboard_host, "--dashboard-port", str(dashboard_port)])

    return argv


def _python_or_frozen_executable() -> str:
    return sys.executable


def _spawn_restart(argv: list[str]) -> None:
    kwargs: dict[str, Any] = {"cwd": os.getcwd()}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen(argv, **kwargs)


async def cmd_run(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    if getattr(args, "backend", None):
        config.backend = args.backend
    if getattr(args, "dashboard_host", None):
        config.dashboard_host = args.dashboard_host
    if getattr(args, "dashboard_port", None):
        config.dashboard_port = args.dashboard_port
    backend = create_backend(config.backend)
    clipboard = create_clipboard() if config.clipboard_enabled else None
    state = StateStore(machine_name=config.machine_name, backend_name=backend.name)
    width, height = backend.screen_size()
    state.update(local_screen_width=width, local_screen_height=height)
    if getattr(backend, "reason", None):
        state.set_error(f"Using null backend because native input is unavailable: {backend.reason}")

    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()
    restart_request: dict[str, list[str] | None] = {"argv": None}
    dashboard: DashboardServer | None = None
    controller_ref: dict[str, BorderController | None] = {"controller": None}
    layout_lock = threading.RLock()
    host_registry: HostClientRegistry | None = None
    if args.command in {"run", "host"}:
        host_registry = HostClientRegistry(state)

    if args.command in {"run", "host"} and not args.no_dashboard:
        auth_tokens = config.accepted_secrets()
        dashboard = DashboardServer(
            config.dashboard_host,
            config.dashboard_port,
            state.snapshot,
            auth_tokens=auth_tokens,
            web_input_handler=_build_web_input_handler(backend, state),
            layout_provider=_build_layout_provider(config, state, layout_lock),
            layout_update_handler=_build_layout_update_handler(
                config,
                args.config,
                state,
                controller_ref,
                host_registry,
                asyncio.get_running_loop(),
                layout_lock,
            ),
            management_handler=_build_management_handler(
                config,
                args.config,
                args,
                state,
                loop,
                stop_event,
                restart_request,
                auth_tokens,
            ),
        )
        dashboard.start()
        print(f"Dashboard: {dashboard.url}")
        print(f"Web controller: {_controller_url(config)}")
        print(f"Layout editor: {_layout_url(config)}")
        print(f"Pairing secret: {config.pairing_secret}")

    agent: AgentServer | None = None
    controller: BorderController | None = None
    connector: ClientConnector | None = None
    tasks: list[asyncio.Task] = []

    if args.command in {"run", "agent", "host"}:
        agent = AgentServer(config, backend, state, host_registry=host_registry, clipboard=clipboard)
        await agent.start()
        tasks.append(asyncio.create_task(agent.serve_forever(), name="mwbc-agent"))

    if args.command == "client":
        connector = ClientConnector(
            config=config,
            backend=backend,
            state=state,
            host=args.host,
            port=args.port,
            retry_seconds=args.retry_seconds,
            clipboard=clipboard,
        )
        await connector.start()

    should_run_controller = args.command == "controller" or (args.command in {"run", "host"} and bool(config.peers))
    if should_run_controller:
        controller = BorderController(config, backend, state, host_registry=host_registry)
        controller_ref["controller"] = controller
        await controller.start()

    mode = args.command
    if args.command == "run" and not config.peers:
        mode = "agent"
    state.update(mode=mode)

    _install_signal_handlers(stop_event)
    print(f"{config.machine_name} running with {backend.name} backend. Press Ctrl+C to stop.")
    try:
        await stop_event.wait()
    except KeyboardInterrupt:
        pass
    finally:
        if connector is not None:
            await connector.stop()
        if controller is not None:
            await controller.stop()
        for task in tasks:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        if agent is not None:
            await agent.stop()
        if dashboard is not None:
            dashboard.stop()
        if restart_request["argv"] is not None:
            _spawn_restart(restart_request["argv"])
    return 0


def _build_web_input_handler(backend: Any, state: StateStore):
    def handle(events: list[dict[str, Any]], client_info: dict[str, Any]) -> int:
        accepted = 0
        client_id = str(client_info.get("id", "browser"))
        short_id = client_id[:8]
        state.update_incoming(
            f"browser:{client_id}",
            machine_name=f"browser-{short_id}",
            peer=client_info.get("peer"),
            active=True,
            screen_width=client_info.get("screen_width"),
            screen_height=client_info.get("screen_height"),
            last_seen=client_info.get("last_seen"),
        )
        for event in events[:128]:
            apply_input_event(backend, event)
            accepted += 1
        state.increment("events_received", accepted)
        return accepted

    return handle


def _controller_url(config: AppConfig) -> str:
    host = config.dashboard_host
    if host in {"0.0.0.0", "::"}:
        host = _guess_lan_ip()
    return f"http://{host}:{config.dashboard_port}/controller#token={config.pairing_secret}"


def _dashboard_url(config: AppConfig) -> str:
    host = config.dashboard_host
    if host in {"0.0.0.0", "::"}:
        host = _guess_lan_ip()
    return f"http://{host}:{config.dashboard_port}"


def _layout_url(config: AppConfig) -> str:
    host = config.dashboard_host
    if host in {"0.0.0.0", "::"}:
        host = _guess_lan_ip()
    return f"http://{host}:{config.dashboard_port}/layout#token={config.pairing_secret}"


def _guess_lan_ip() -> str:
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("8.8.8.8", 80))
        return str(probe.getsockname()[0])
    except OSError:
        return socket.gethostbyname(socket.gethostname())
    finally:
        probe.close()


def _install_signal_handlers(stop_event: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()
    for signame in ("SIGINT", "SIGTERM"):
        sig = getattr(signal, signame, None)
        if sig is None:
            continue
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop_event.set)


if __name__ == "__main__":
    raise SystemExit(main())
