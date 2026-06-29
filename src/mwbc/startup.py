from __future__ import annotations

import os
import platform
import plistlib
import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from .config import DEFAULT_CONFIG_PATH

APP_ID = "com.localcodex.mwbc"
APP_NAME = "MWBC"
WINDOWS_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
MACOS_LAUNCH_AGENT = f"{APP_ID}.plist"


class StartupError(RuntimeError):
    """Raised when startup registration cannot be changed."""


@dataclass(frozen=True, slots=True)
class StartupOptions:
    mode: str = "run"
    config_path: Path = DEFAULT_CONFIG_PATH
    host: str | None = None
    port: int = 45445
    retry_seconds: float = 1.0
    no_dashboard: bool = False
    dashboard_host: str | None = None
    dashboard_port: int | None = None
    backend: str | None = None
    log_level: str = "INFO"
    keep_alive: bool = False


@dataclass(frozen=True, slots=True)
class StartupStatus:
    supported: bool
    installed: bool
    platform_name: str
    location: str | None = None
    command: str | None = None
    message: str | None = None


def install_startup(options: StartupOptions) -> StartupStatus:
    system = platform.system()
    if system == "Windows":
        return _install_windows(options)
    if system == "Darwin":
        return _install_macos(options)
    raise StartupError(f"startup registration is not implemented for {system or 'this platform'}")


def uninstall_startup() -> StartupStatus:
    system = platform.system()
    if system == "Windows":
        return _uninstall_windows()
    if system == "Darwin":
        return _uninstall_macos()
    raise StartupError(f"startup registration is not implemented for {system or 'this platform'}")


def startup_status() -> StartupStatus:
    system = platform.system()
    if system == "Windows":
        return _status_windows()
    if system == "Darwin":
        return _status_macos()
    return StartupStatus(
        supported=False,
        installed=False,
        platform_name=system or "unknown",
        message="startup registration is only implemented for Windows and macOS",
    )


def build_startup_argv(options: StartupOptions) -> list[str]:
    if options.mode not in {"run", "agent", "controller", "host", "client"}:
        raise StartupError("startup mode must be run, agent, controller, host, or client")
    if options.mode == "client" and not options.host:
        raise StartupError("client startup mode requires --host")

    argv = [_startup_executable()]
    if not getattr(sys, "frozen", False):
        argv.extend(["-m", "mwbc"])
    argv.extend(
        [
            "--config",
            str(options.config_path.expanduser()),
            "--log-level",
            options.log_level,
            options.mode,
        ]
    )
    if options.mode == "client":
        assert options.host is not None
        argv.extend(["--host", options.host, "--port", str(options.port)])
        if options.retry_seconds != 1.0:
            argv.extend(["--retry-seconds", str(options.retry_seconds)])
    if options.backend:
        argv.extend(["--backend", options.backend])
    if options.mode in {"run", "host"}:
        if options.no_dashboard:
            argv.append("--no-dashboard")
        if options.dashboard_host:
            argv.extend(["--dashboard-host", options.dashboard_host])
        if options.dashboard_port is not None:
            argv.extend(["--dashboard-port", str(options.dashboard_port)])
    return argv


def windows_command_line(options: StartupOptions) -> str:
    return subprocess.list2cmdline(build_startup_argv(options))


def macos_plist(options: StartupOptions) -> dict:
    plist: dict = {
        "Label": APP_ID,
        "ProgramArguments": build_startup_argv(options),
        "RunAtLoad": True,
        "StandardOutPath": str(_log_dir() / "mwbc.out.log"),
        "StandardErrorPath": str(_log_dir() / "mwbc.err.log"),
        "WorkingDirectory": str(Path.home()),
    }
    if options.keep_alive:
        plist["KeepAlive"] = {"SuccessfulExit": False}
    return plist


def _startup_executable() -> str:
    executable = Path(sys.executable)
    if platform.system() == "Windows":
        pythonw = executable.with_name("pythonw.exe")
        if pythonw.exists():
            return str(pythonw)
    return str(executable)


def _install_windows(options: StartupOptions) -> StartupStatus:
    try:
        import winreg
    except ImportError as exc:
        raise StartupError("winreg is unavailable; Windows startup can only be installed on Windows") from exc

    command = windows_command_line(options)
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, WINDOWS_RUN_KEY) as key:
        winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, command)
    return StartupStatus(
        supported=True,
        installed=True,
        platform_name="Windows",
        location=rf"HKCU\{WINDOWS_RUN_KEY}\{APP_NAME}",
        command=command,
    )


def _uninstall_windows() -> StartupStatus:
    try:
        import winreg
    except ImportError as exc:
        raise StartupError("winreg is unavailable; Windows startup can only be uninstalled on Windows") from exc

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, WINDOWS_RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, APP_NAME)
    except FileNotFoundError:
        pass
    return StartupStatus(
        supported=True,
        installed=False,
        platform_name="Windows",
        location=rf"HKCU\{WINDOWS_RUN_KEY}\{APP_NAME}",
    )


def _status_windows() -> StartupStatus:
    try:
        import winreg
    except ImportError:
        return StartupStatus(False, False, "Windows", message="winreg is unavailable")

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, WINDOWS_RUN_KEY) as key:
            command, _ = winreg.QueryValueEx(key, APP_NAME)
    except FileNotFoundError:
        return StartupStatus(
            supported=True,
            installed=False,
            platform_name="Windows",
            location=rf"HKCU\{WINDOWS_RUN_KEY}\{APP_NAME}",
        )
    return StartupStatus(
        supported=True,
        installed=True,
        platform_name="Windows",
        location=rf"HKCU\{WINDOWS_RUN_KEY}\{APP_NAME}",
        command=str(command),
    )


def _install_macos(options: StartupOptions) -> StartupStatus:
    path = _macos_plist_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    _log_dir().mkdir(parents=True, exist_ok=True)
    plist = macos_plist(options)
    path.write_bytes(plistlib.dumps(plist, sort_keys=False))
    _launchctl("bootout", path, check=False)
    _launchctl("bootstrap", path, check=False)
    return StartupStatus(
        supported=True,
        installed=True,
        platform_name="macOS",
        location=str(path),
        command=shlex.join(plist["ProgramArguments"]),
    )


def _uninstall_macos() -> StartupStatus:
    path = _macos_plist_path()
    _launchctl("bootout", path, check=False)
    if path.exists():
        path.unlink()
    return StartupStatus(
        supported=True,
        installed=False,
        platform_name="macOS",
        location=str(path),
    )


def _status_macos() -> StartupStatus:
    path = _macos_plist_path()
    if not path.exists():
        return StartupStatus(
            supported=True,
            installed=False,
            platform_name="macOS",
            location=str(path),
        )
    try:
        plist = plistlib.loads(path.read_bytes())
    except plistlib.InvalidFileException:
        return StartupStatus(
            supported=True,
            installed=True,
            platform_name="macOS",
            location=str(path),
            message="plist exists but could not be parsed",
        )
    command = shlex.join(str(item) for item in plist.get("ProgramArguments", []))
    return StartupStatus(
        supported=True,
        installed=True,
        platform_name="macOS",
        location=str(path),
        command=command,
    )


def _macos_plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / MACOS_LAUNCH_AGENT


def _log_dir() -> Path:
    if platform.system() == "Darwin":
        return Path.home() / "Library" / "Logs" / "MWBC"
    if platform.system() == "Windows":
        return Path(os.environ.get("LOCALAPPDATA", Path.home())) / "MWBC" / "Logs"
    return Path.home() / ".mwbc" / "logs"


def _launchctl(action: str, plist_path: Path, *, check: bool) -> None:
    if platform.system() != "Darwin":
        return
    domain = f"gui/{os.getuid()}"
    command = ["launchctl", action, domain, str(plist_path)]
    try:
        subprocess.run(command, check=check, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except (OSError, subprocess.CalledProcessError) as exc:
        if check:
            raise StartupError(f"launchctl {action} failed") from exc
