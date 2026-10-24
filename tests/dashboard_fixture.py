"""Disposable dashboard for browser tests; never loads a user's configuration."""
from __future__ import annotations

import asyncio
import json
import signal
import tempfile
import threading
from pathlib import Path

from mwbc.cli import _build_layout_provider, _build_layout_update_handler
from mwbc.config import AppConfig, PeerConfig, save_config
from mwbc.dashboard import DashboardServer
from mwbc.state import StateStore


async def main() -> None:
    with tempfile.TemporaryDirectory(prefix="mwbc-browser-test-") as directory:
        path = Path(directory) / "config.json"
        config = AppConfig(machine_name="TEST HOST", peers=[
            PeerConfig(name="MACBOOK", edge="left"),
            PeerConfig(name="WINDOWS2", edge="top"),
        ])
        save_config(config, path)
        state = StateStore(config.machine_name, "null")
        state.update(mode="host")
        lock = threading.RLock()
        server = DashboardServer(
            "127.0.0.1", 0, state.snapshot, auth_tokens=["fixture-only"],
            layout_provider=_build_layout_provider(config, state, lock),
            layout_update_handler=_build_layout_update_handler(
                config, path, state, {"controller": None}, None, asyncio.get_running_loop(), lock),
        )
        stopped = asyncio.Event()
        for event in (signal.SIGINT, signal.SIGTERM):
            asyncio.get_running_loop().add_signal_handler(event, stopped.set)
        server.start()
        try:
            print(json.dumps({"url": f"http://127.0.0.1:{server._httpd.server_address[1]}"}), flush=True)
            await stopped.wait()
        finally:
            server.stop()


if __name__ == "__main__":
    asyncio.run(main())
