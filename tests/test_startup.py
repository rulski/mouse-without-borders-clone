from __future__ import annotations

import plistlib
import platform
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from mwbc.startup import APP_ID, StartupOptions, build_startup_argv, macos_plist, startup_status, windows_command_line


def _expected_startup_executable() -> str:
    executable = Path(sys.executable)
    if platform.system() == "Windows":
        pythonw = executable.with_name("pythonw.exe")
        if pythonw.exists():
            return str(pythonw)
    return str(executable)


class StartupTests(unittest.TestCase):
    def test_build_agent_startup_argv(self) -> None:
        config_path = Path("/tmp/mwbc config.json")
        argv = build_startup_argv(
            StartupOptions(
                mode="agent",
                config_path=config_path,
                backend="null",
                log_level="DEBUG",
            )
        )
        self.assertEqual(argv[0], _expected_startup_executable())
        self.assertEqual(argv[1:7], ["-m", "mwbc", "--config", str(config_path), "--log-level", "DEBUG"])
        self.assertEqual(argv[-3:], ["agent", "--backend", "null"])

    def test_build_run_startup_argv_with_dashboard_options(self) -> None:
        argv = build_startup_argv(
            StartupOptions(
                mode="run",
                config_path=Path("/tmp/config.json"),
                no_dashboard=True,
                dashboard_host="0.0.0.0",
                dashboard_port=45446,
            )
        )
        self.assertIn("run", argv)
        self.assertIn("--no-dashboard", argv)
        self.assertIn("--dashboard-host", argv)
        self.assertIn("0.0.0.0", argv)
        self.assertIn("--dashboard-port", argv)
        self.assertIn("45446", argv)

    def test_build_client_startup_argv_requires_host(self) -> None:
        with self.assertRaises(Exception):
            build_startup_argv(StartupOptions(mode="client"))

    def test_build_client_startup_argv(self) -> None:
        argv = build_startup_argv(
            StartupOptions(
                mode="client",
                config_path=Path("/tmp/config.json"),
                host="desktop.local",
                port=45445,
                retry_seconds=0.5,
            )
        )
        self.assertIn("client", argv)
        self.assertIn("--host", argv)
        self.assertIn("desktop.local", argv)
        self.assertIn("--retry-seconds", argv)
        self.assertIn("0.5", argv)

    def test_frozen_startup_argv_uses_executable_directly(self) -> None:
        with patch.object(sys, "frozen", True, create=True):
            argv = build_startup_argv(StartupOptions(mode="agent", config_path=Path("/tmp/config.json")))
        self.assertEqual(argv[0], _expected_startup_executable())
        self.assertNotIn("-m", argv)
        self.assertNotIn("mwbc", argv)
        self.assertIn("agent", argv)

    def test_windows_command_quotes_paths_with_spaces(self) -> None:
        config_path = Path("C:/Users/Test User/mwbc.json")
        command = windows_command_line(StartupOptions(mode="agent", config_path=config_path))
        self.assertIn("-m mwbc", command)
        self.assertIn(f'"{config_path}"', command)

    def test_macos_plist_is_serializable(self) -> None:
        plist = macos_plist(StartupOptions(mode="agent", keep_alive=True))
        self.assertEqual(plist["Label"], APP_ID)
        self.assertTrue(plist["RunAtLoad"])
        self.assertEqual(plist["KeepAlive"], {"SuccessfulExit": False})
        self.assertIn("ProgramArguments", plist)
        self.assertIn("agent", plist["ProgramArguments"])
        self.assertIsInstance(plistlib.dumps(plist), bytes)

    def test_startup_status_reports_unsupported_platform_here(self) -> None:
        status = startup_status()
        if status.platform_name not in {"Windows", "Darwin"}:
            self.assertFalse(status.supported)


if __name__ == "__main__":
    unittest.main()
