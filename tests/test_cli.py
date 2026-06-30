from __future__ import annotations

import asyncio
import tempfile
import threading
import unittest
from pathlib import Path

from mwbc.cli import _build_layout_update_handler
from mwbc.config import AppConfig, PeerConfig, load_config
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
                            }
                        ]
                    }
                )

                self.assertEqual(result["peers"][0]["edge"], "top")
                self.assertTrue(result["peers"][0]["keep_awake"])
                self.assertEqual(result["peers"][0]["keep_awake_interval_seconds"], 30)

                saved = load_config(config_path)
                self.assertEqual(saved.peers[0].edge, "top")
                self.assertTrue(saved.peers[0].keep_awake)
                self.assertEqual(saved.peers[0].keep_awake_interval_seconds, 30)
            finally:
                loop.close()


if __name__ == "__main__":
    unittest.main()
