from __future__ import annotations

import shutil
import subprocess
import sys
from typing import Protocol


class Clipboard(Protocol):
    def get_text(self) -> str:
        ...

    def set_text(self, text: str) -> None:
        ...


class ClipboardError(RuntimeError):
    pass


class SystemClipboard:
    def get_text(self) -> str:
        if sys.platform.startswith("win"):
            return _windows_get_text()
        if sys.platform == "darwin":
            return _run_clipboard_command(["pbpaste"])
        return _linux_get_text()

    def set_text(self, text: str) -> None:
        if sys.platform.startswith("win"):
            _windows_set_text(text)
            return
        if sys.platform == "darwin":
            _run_clipboard_command(["pbcopy"], input_text=text)
            return
        _linux_set_text(text)


class NullClipboard:
    def __init__(self, text: str = "") -> None:
        self.text = text

    def get_text(self) -> str:
        return self.text

    def set_text(self, text: str) -> None:
        self.text = text


def create_clipboard() -> Clipboard:
    return SystemClipboard()


def truncate_text(text: str, max_bytes: int) -> str:
    if max_bytes <= 0:
        return ""
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    return encoded[:max_bytes].decode("utf-8", errors="ignore")


def _run_clipboard_command(command: list[str], input_text: str | None = None) -> str:
    try:
        result = subprocess.run(
            command,
            input=input_text,
            capture_output=True,
            check=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ClipboardError(f"clipboard command failed: {' '.join(command)}") from exc
    return result.stdout


def _linux_get_text() -> str:
    for command in (
        ["wl-paste", "--no-newline"],
        ["xclip", "-selection", "clipboard", "-out"],
        ["xsel", "--clipboard", "--output"],
    ):
        if shutil.which(command[0]):
            return _run_clipboard_command(command)
    raise ClipboardError("no supported Linux clipboard command found")


def _linux_set_text(text: str) -> None:
    for command in (
        ["wl-copy"],
        ["xclip", "-selection", "clipboard", "-in"],
        ["xsel", "--clipboard", "--input"],
    ):
        if shutil.which(command[0]):
            _run_clipboard_command(command, input_text=text)
            return
    raise ClipboardError("no supported Linux clipboard command found")


def _windows_get_text() -> str:
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    cf_unicode_text = 13

    user32.OpenClipboard.argtypes = [wintypes.HWND]
    user32.OpenClipboard.restype = wintypes.BOOL
    user32.CloseClipboard.argtypes = []
    user32.CloseClipboard.restype = wintypes.BOOL
    user32.IsClipboardFormatAvailable.argtypes = [wintypes.UINT]
    user32.IsClipboardFormatAvailable.restype = wintypes.BOOL
    user32.GetClipboardData.argtypes = [wintypes.UINT]
    user32.GetClipboardData.restype = wintypes.HANDLE
    kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalUnlock.restype = wintypes.BOOL

    if not user32.IsClipboardFormatAvailable(cf_unicode_text):
        return ""
    if not user32.OpenClipboard(None):
        raise ClipboardError("could not open Windows clipboard")
    try:
        handle = user32.GetClipboardData(cf_unicode_text)
        if not handle:
            return ""
        pointer = kernel32.GlobalLock(handle)
        if not pointer:
            return ""
        try:
            return ctypes.wstring_at(pointer)
        finally:
            kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()


def _windows_set_text(text: str) -> None:
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    cf_unicode_text = 13
    gmem_moveable = 0x0002

    user32.OpenClipboard.argtypes = [wintypes.HWND]
    user32.OpenClipboard.restype = wintypes.BOOL
    user32.EmptyClipboard.argtypes = []
    user32.EmptyClipboard.restype = wintypes.BOOL
    user32.CloseClipboard.argtypes = []
    user32.CloseClipboard.restype = wintypes.BOOL
    user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
    user32.SetClipboardData.restype = wintypes.HANDLE
    kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
    kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalUnlock.restype = wintypes.BOOL
    kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalFree.restype = wintypes.HGLOBAL

    data = text + "\0"
    buffer = ctypes.create_unicode_buffer(data)
    size = ctypes.sizeof(buffer)
    handle = kernel32.GlobalAlloc(gmem_moveable, size)
    if not handle:
        raise ClipboardError("could not allocate Windows clipboard memory")

    pointer = kernel32.GlobalLock(handle)
    if not pointer:
        kernel32.GlobalFree(handle)
        raise ClipboardError("could not lock Windows clipboard memory")
    try:
        ctypes.memmove(pointer, buffer, size)
    finally:
        kernel32.GlobalUnlock(handle)

    if not user32.OpenClipboard(None):
        kernel32.GlobalFree(handle)
        raise ClipboardError("could not open Windows clipboard")
    try:
        if not user32.EmptyClipboard():
            raise ClipboardError("could not empty Windows clipboard")
        if not user32.SetClipboardData(cf_unicode_text, handle):
            raise ClipboardError("could not set Windows clipboard data")
        handle = None
    finally:
        user32.CloseClipboard()
        if handle:
            kernel32.GlobalFree(handle)
