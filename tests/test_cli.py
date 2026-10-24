from __future__ import annotations

import argparse
import asyncio
import json
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from pathlib import Path

from mwbc.cli import _build_layout_update_handler, _build_management_handler, _service_status_payload
from mwbc.config import AppConfig, PeerConfig, load_config, save_config
from mwbc.state import StateStore


class CliLayoutTests(unittest.TestCase):
    def test_layout_invalid_candidates_do_not_change_memory_disk_or_status(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            path = Path(tempdir) / "config.json"
            config = AppConfig(peers=[PeerConfig(name="MACBOOK", edge="left"), PeerConfig(name="PC2", edge="top")])
            save_config(config, path)
            state = StateStore("HOST", "null")
            loop = asyncio.new_event_loop()
            self.addCleanup(loop.close)
            handler = _build_layout_update_handler(config, path, state, {"controller": None}, None, loop, threading.RLock())
            before = path.read_bytes()
            memory = config.to_dict()
            snapshot = state.snapshot()["peers"]
            invalid = [
                [{"name": "PC2", "edge": "left"}],
                [{"name": "MACBOOK", "edge": "bottom"}, {"name": "MISSING", "edge": "right"}],
                [{"name": "MACBOOK", "edge": "bottom"}, {"name": "MACBOOK", "edge": "right"}],
                [{"name": "MACBOOK", "edge": "bottom", "keep_awake_interval_seconds": float("nan")}],
            ]
            for peers in invalid:
                with self.subTest(peers=peers), self.assertRaises(ValueError):
                    handler({"peers": peers})
                self.assertEqual(config.to_dict(), memory)
                self.assertEqual(path.read_bytes(), before)
                self.assertEqual(state.snapshot()["peers"], snapshot)

    def test_layout_disk_failure_does_not_change_running_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            path = Path(tempdir) / "config.json"
            config = AppConfig(peers=[PeerConfig(name="MACBOOK", edge="left")])
            save_config(config, path)
            loop = asyncio.new_event_loop()
            self.addCleanup(loop.close)
            handler = _build_layout_update_handler(config, path, StateStore("HOST", "null"), {"controller": None}, None, loop, threading.RLock())
            before = path.read_bytes()
            with patch("mwbc.cli.save_config", side_effect=OSError("disk full")), self.assertRaises(OSError):
                handler({"peers": [{"name": "MACBOOK", "edge": "top"}]})
            self.assertEqual(config.peers[0].edge, "left")
            self.assertEqual(path.read_bytes(), before)

    def test_active_device_cannot_move_until_control_returns_home(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            path = Path(tempdir) / "config.json"
            config = AppConfig(peers=[PeerConfig(name="MACBOOK", edge="left")])
            save_config(config, path)
            loop = asyncio.new_event_loop()
            self.addCleanup(loop.close)
            controller = SimpleNamespace(active=SimpleNamespace(peer=config.peers[0]))
            handler = _build_layout_update_handler(config, path, StateStore("HOST", "null"), {"controller": controller}, None, loop, threading.RLock())
            with self.assertRaisesRegex(ValueError, "F12"):
                handler({"peers": [{"name": "MACBOOK", "edge": "top"}]})
            self.assertEqual(controller.active.peer.edge, "left")
            self.assertEqual(load_config(path).peers[0].edge, "left")
            controller.active = None
            handler({"peers": [{"name": "MACBOOK", "edge": "top"}]})
            self.assertEqual(config.peers[0].edge, "top")

    def test_layout_can_swap_two_edges_in_one_save(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            config = AppConfig(peers=[PeerConfig(name="MACBOOK", edge="left"), PeerConfig(name="PC2", edge="top")])
            loop = asyncio.new_event_loop()
            self.addCleanup(loop.close)
            handler = _build_layout_update_handler(config, Path(tempdir) / "config.json", StateStore("HOST", "null"), {"controller": None}, None, loop, threading.RLock())
            result = handler({"peers": [{"name": "MACBOOK", "edge": "top"}, {"name": "PC2", "edge": "left"}]})
            self.assertEqual([peer["edge"] for peer in result["peers"]], ["top", "left"])

    def test_layout_handler_persists_peer_feature_settings(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            config_path = Path(tempdir) / "config.json"
            config = AppConfig(
                machine_name="HOST",
                pairing_secret="secret",
                peers=[PeerConfig(name="MACBOOK", edge="left")],
            )
            state = StateStore("HOST", "null")
            state.register_peer("MACBOOK", "", 45445, "left")
            loop = asyncio.new_event_loop()
            try:
                handler = _build_layout_update_handler(
                    config,
                    config_path,
                    state,
                    {"controller": None},
                    None,
                    loop,
                    threading.RLock(),
                )

                result = handler(
                    {
                        "peers": [
                            {
                                "name": "MACBOOK",
                                "edge": "top",
                                "keep_awake": True,
                                "keep_awake_interval_seconds": 30,
                                "scroll_multiplier": 3.0,
                            }
                        ]
                    }
                )

                self.assertEqual(result["peers"][0]["edge"], "top")
                self.assertTrue(result["peers"][0]["keep_awake"])
                self.assertEqual(result["peers"][0]["keep_awake_interval_seconds"], 30)
                self.assertEqual(result["peers"][0]["scroll_multiplier"], 3.0)

                saved = load_config(config_path)
                self.assertEqual(saved.peers[0].edge, "top")
                self.assertTrue(saved.peers[0].keep_awake)
                self.assertEqual(saved.peers[0].keep_awake_interval_seconds, 30)
                self.assertEqual(saved.peers[0].scroll_multiplier, 3.0)
            finally:
                loop.close()

    def test_management_handler_regenerates_pairing_secret(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            config_path = Path(tempdir) / "config.json"
            config = AppConfig(machine_name="HOST", pairing_secret="old-secret")
            save_config(config, config_path)
            state = StateStore("HOST", "null")
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            stop_event = asyncio.Event()
            auth_tokens = config.accepted_secrets()
            try:
                handler = _build_management_handler(
                    config,
                    config_path,
                    argparse.Namespace(
                        command="host",
                        config=config_path,
                        log_level="INFO",
                        backend=None,
                        no_dashboard=False,
                    ),
                    state,
                    loop,
                    stop_event,
                    {"argv": None},
                    auth_tokens,
                )

                result = handler("secret.regenerate", {})

                self.assertNotEqual(result["pairing_secret"], "old-secret")
                self.assertEqual(config.pairing_secret, result["pairing_secret"])
                self.assertNotIn(result["pairing_secret"], auth_tokens)
                self.assertEqual(auth_tokens, [config.api_token])
                self.assertNotIn("old-secret", auth_tokens)
                self.assertEqual(load_config(config_path).pairing_secret, result["pairing_secret"])
            finally:
                asyncio.set_event_loop(None)
                loop.close()

    def test_public_service_status_does_not_include_token_urls(self) -> None:
        config = AppConfig(machine_name="HOST", pairing_secret="secret")
        state = StateStore("HOST", "null", device_id=config.device_id)
        args = argparse.Namespace(command="host", config=Path("config.json"))

        payload = _service_status_payload(config, args, state, include_private=False)

        serialized = json.dumps(payload)
        self.assertNotIn("secret", serialized)
        self.assertIsNone(payload["layout_url"])
        self.assertIsNone(payload["controller_url"])
        self.assertFalse(payload["authenticated"])

    def test_authenticated_service_status_includes_token_urls(self) -> None:
        config = AppConfig(machine_name="HOST", pairing_secret="secret")
        state = StateStore("HOST", "null", device_id=config.device_id)
        args = argparse.Namespace(command="host", config=Path("config.json"))

        payload = _service_status_payload(config, args, state, include_private=True)

        self.assertIn("#token=" + config.api_token, payload["layout_url"])
        self.assertIsNone(payload["controller_url"])
        self.assertNotIn("#token=secret", payload["layout_url"])
        self.assertTrue(payload["authenticated"])

    def test_load_config_migrates_missing_device_id(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            config_path = Path(tempdir) / "config.json"
            config_path.write_text(
                json.dumps({"machine_name": "HOST", "pairing_secret": "secret", "peers": []}),
                encoding="utf-8",
            )

            config = load_config(config_path)

            self.assertTrue(config.device_id)
            self.assertEqual(load_config(config_path).device_id, config.device_id)


class CliLayoutLoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_dashboard_thread_checks_active_session_on_controller_loop(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            path = Path(tempdir) / "config.json"
            config = AppConfig(peers=[PeerConfig(name="MACBOOK", edge="left")])
            save_config(config, path)
            controller = SimpleNamespace(active=SimpleNamespace(peer=config.peers[0]), refresh_peers=AsyncMock())
            handler = _build_layout_update_handler(config, path, StateStore("HOST", "null"), {"controller": controller}, None, asyncio.get_running_loop(), threading.RLock())
            with self.assertRaisesRegex(ValueError, "F12"):
                await asyncio.to_thread(handler, {"peers": [{"name": "MACBOOK", "edge": "top"}]})
            self.assertEqual(load_config(path).peers[0].edge, "left")
            controller.active = None
            await asyncio.to_thread(handler, {"peers": [{"name": "MACBOOK", "edge": "top"}]})
            await asyncio.sleep(0)
            self.assertEqual(load_config(path).peers[0].edge, "top")
            controller.refresh_peers.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
