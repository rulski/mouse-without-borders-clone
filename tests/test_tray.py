from __future__ import annotations

import unittest
from pathlib import Path

from mwbc.tray import build_start_argv, dashboard_url, layout_url, normalize_base_url, summarize_status


class TrayTests(unittest.TestCase):
    def test_url_helpers_normalize_dashboard_and_layout_urls(self) -> None:
        self.assertEqual(normalize_base_url("http://127.0.0.1:45446/"), "http://127.0.0.1:45446")
        self.assertEqual(dashboard_url("http://127.0.0.1:45446/"), "http://127.0.0.1:45446")
        self.assertEqual(
            layout_url("http://127.0.0.1:45446/", "secret"),
            "http://127.0.0.1:45446/layout#token=secret",
        )

    def test_status_summary_reports_offline_api(self) -> None:
        snapshot = summarize_status(None, None, "connection refused")

        self.assertFalse(snapshot.online)
        self.assertEqual(snapshot.status_text, "MWBC offline")
        self.assertEqual(snapshot.error, "connection refused")

    def test_status_summary_reports_connected_devices(self) -> None:
        snapshot = summarize_status(
            {
                "machine_name": "HOST",
                "mode": "host",
                "active_peer": None,
                "peers": [
                    {"name": "MACBOOK", "connected": True},
                    {"name": "WINDOWS2", "connected": False},
                ],
            },
            {"mode": "host"},
        )

        self.assertTrue(snapshot.online)
        self.assertEqual(snapshot.status_text, "MWBC connected: MACBOOK")
        self.assertEqual(snapshot.connected_peers, ("MACBOOK",))
        self.assertFalse(snapshot.host_lock)

    def test_status_summary_counts_connected_incoming_clients_even_when_inactive(self) -> None:
        snapshot = summarize_status(
            {
                "machine_name": "HOST",
                "mode": "host",
                "incoming_clients": {
                    "MACBOOK ('192.168.1.33', 51413)": {
                        "machine_name": "MACBOOK",
                        "connected": True,
                        "active": False,
                    }
                },
            },
            None,
        )

        self.assertEqual(snapshot.connected_peers, ("MACBOOK",))
        self.assertEqual(snapshot.status_text, "MWBC connected: MACBOOK")

    def test_status_summary_reports_host_lock(self) -> None:
        snapshot = summarize_status(
            {
                "machine_name": "HOST",
                "mode": "host",
                "edge_switching_paused": True,
                "host_lock_hotkey": "F12",
                "peers": [{"name": "MACBOOK", "connected": True}],
            },
            None,
        )

        self.assertTrue(snapshot.online)
        self.assertTrue(snapshot.host_lock)
        self.assertEqual(snapshot.status_text, "MWBC locked to host")
        self.assertIn("F12 lock on", snapshot.detail_text)

    def test_build_start_argv_uses_config_and_dashboard_settings(self) -> None:
        config_path = Path("mwbc-config.json")
        argv = build_start_argv(
            config_path=config_path,
            log_level="DEBUG",
            mode="host",
            backend="pynput",
            dashboard_host="127.0.0.1",
            dashboard_port=45446,
        )

        self.assertIn("-m", argv)
        self.assertIn("mwbc", argv)
        self.assertIn("--config", argv)
        self.assertIn(str(config_path), argv)
        self.assertIn("--backend", argv)
        self.assertIn("pynput", argv)
        self.assertIn("--dashboard-port", argv)
        self.assertIn("45446", argv)


if __name__ == "__main__":
    unittest.main()
