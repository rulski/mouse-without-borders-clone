from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import signal
import socket
import threading
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
from .controller import BorderController
from .dashboard import DashboardServer
from .input_backend import apply_input_event, create_backend
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
    layout_lock: threading.RLock,
):
    def handle(payload: dict[str, Any]) -> dict[str, Any]:
        requested = payload.get("peers")
        if not isinstance(requested, list) or not all(isinstance(item, dict) for item in requested):
            raise ValueError("peers must be a list of objects")

        edge_by_name: dict[str, str] = {}
        for item in requested:
            name = str(item.get("name") or "").strip()
            edge = str(item.get("edge") or "").lower().strip()
            if not name:
                raise ValueError("peer is missing a name")
            if edge not in VALID_EDGES:
                raise ValueError(f"invalid edge {edge!r}; expected one of {sorted(VALID_EDGES)}")
            edge_by_name[name] = edge

        with layout_lock:
            known = {peer.name: peer for peer in config.peers}
            unknown = sorted(name for name in edge_by_name if name not in known)
            if unknown:
                raise ValueError(f"unknown peer: {', '.join(unknown)}")

            for name, edge in edge_by_name.items():
                known[name].edge = edge
            save_config(config, config_path)
            for peer in config.peers:
                state.register_peer(peer.name, peer.host, peer.port, peer.edge)

        controller = controller_ref.get("controller")
        if controller is not None and controller.loop is not None and controller.loop.is_running():
            future = asyncio.run_coroutine_threadsafe(controller.refresh_peers(), controller.loop)
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
            }
        )
    return {"machine_name": config.machine_name, "peers": peers}


async def cmd_run(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    if getattr(args, "backend", None):
        config.backend = args.backend
    if getattr(args, "dashboard_host", None):
        config.dashboard_host = args.dashboard_host
    if getattr(args, "dashboard_port", None):
        config.dashboard_port = args.dashboard_port
    backend = create_backend(config.backend)
    state = StateStore(machine_name=config.machine_name, backend_name=backend.name)
    width, height = backend.screen_size()
    state.update(local_screen_width=width, local_screen_height=height)
    if getattr(backend, "reason", None):
        state.set_error(f"Using null backend because native input is unavailable: {backend.reason}")

    dashboard: DashboardServer | None = None
    controller_ref: dict[str, BorderController | None] = {"controller": None}
    layout_lock = threading.RLock()
    if args.command in {"run", "host"} and not args.no_dashboard:
        dashboard = DashboardServer(
            config.dashboard_host,
            config.dashboard_port,
            state.snapshot,
            auth_tokens=config.accepted_secrets(),
            web_input_handler=_build_web_input_handler(backend, state),
            layout_provider=_build_layout_provider(config, state, layout_lock),
            layout_update_handler=_build_layout_update_handler(config, args.config, state, controller_ref, layout_lock),
        )
        dashboard.start()
        print(f"Dashboard: {dashboard.url}")
        print(f"Web controller: {_controller_url(config)}")
        print(f"Layout editor: {_layout_url(config)}")

    agent: AgentServer | None = None
    controller: BorderController | None = None
    connector: ClientConnector | None = None
    host_registry: HostClientRegistry | None = None
    tasks: list[asyncio.Task] = []

    if args.command in {"run", "host"}:
        host_registry = HostClientRegistry(state)

    if args.command in {"run", "agent", "host"}:
        agent = AgentServer(config, backend, state, host_registry=host_registry)
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

    stop_event = asyncio.Event()
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
