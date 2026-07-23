from __future__ import annotations

import argparse
import asyncio
import tempfile
import threading
import unittest
from pathlib import Path

from mwbc.cli import _build_layout_update_handler, _build_management_handler
from mwbc.config import AppConfig, PeerConfig, load_config, save_config
from mwbc.state import StateStore


class CliLayoutTests(unittest.TestCase):
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
                self.assertIn(result["pairing_secret"], auth_tokens)
                self.assertNotIn("old-secret", auth_tokens)
                self.assertEqual(load_config(config_path).pairing_secret, result["pairing_secret"])
            finally:
                asyncio.set_event_loop(None)
                loop.close()


if __name__ == "__main__":
    unittest.main()
