from __future__ import annotations

import base64
import hashlib
import shutil
import subprocess
import sys
from dataclasses import dataclass
from io import BytesIO
from typing import Any
from typing import Protocol


class Clipboard(Protocol):
    def get_text(self) -> str:
        ...

    def set_text(self, text: str) -> None:
        ...

    def get_image_png(self) -> bytes | None:
        ...

    def set_image_png(self, data: bytes) -> None:
        ...


class ClipboardError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ClipboardPayload:
    kind: str
    text: str = ""
    mime: str = ""
    data: bytes = b""
    digest: str = ""

    @classmethod
    def from_text(cls, text: str) -> "ClipboardPayload":
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        return cls(kind="text", text=text, mime="text/plain", digest=digest)

    @classmethod
    def from_image_png(cls, data: bytes) -> "ClipboardPayload":
        return cls(
            kind="image",
            mime="image/png",
            data=data,
            digest=_image_content_digest(data),
        )

    @property
    def signature(self) -> str:
        return f"{self.kind}:{self.mime}:{self.digest}"

    def to_wire_payload(self, source: str) -> dict[str, Any]:
        if self.kind == "image":
            return {
                "source": source,
                "kind": "image",
                "mime": "image/png",
                "bytes": len(self.data),
                "sha256": self.digest,
                "data_b64": base64.b64encode(self.data).decode("ascii"),
                # Older MWBC versions expect this key to exist.
                "text": "",
            }
        return {
            "source": source,
            "kind": "text",
            "text": self.text,
            "sha256": self.digest,
        }


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

    def get_image_png(self) -> bytes | None:
        if sys.platform.startswith("win"):
            return _pillow_grab_clipboard_image_png()
        if sys.platform == "darwin":
            return _macos_get_image_png()
        return _linux_get_image_png()

    def set_image_png(self, data: bytes) -> None:
        if sys.platform.startswith("win"):
            _windows_set_image_png(data)
            return
        if sys.platform == "darwin":
            _macos_set_image_png(data)
            return
        _linux_set_image_png(data)


class NullClipboard:
    def __init__(self, text: str = "", image_png: bytes | None = None) -> None:
        self.text = text
        self.image_png = image_png

    def get_text(self) -> str:
        return self.text

    def set_text(self, text: str) -> None:
        self.text = text
        self.image_png = None

    def get_image_png(self) -> bytes | None:
        return self.image_png

    def set_image_png(self, data: bytes) -> None:
        self.text = ""
        self.image_png = data


def create_clipboard() -> Clipboard:
    return SystemClipboard()


def truncate_text(text: str, max_bytes: int) -> str:
    if max_bytes <= 0:
        return ""
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    return encoded[:max_bytes].decode("utf-8", errors="ignore")


def read_clipboard_payload(
    clipboard: Clipboard,
    max_text_bytes: int,
    max_image_bytes: int,
) -> ClipboardPayload:
    image = clipboard.get_image_png()
    if image:
        if len(image) > max_image_bytes:
            raise ClipboardError(
                f"clipboard image is {len(image)} bytes; limit is {max_image_bytes} bytes"
            )
        return ClipboardPayload.from_image_png(image)
    return ClipboardPayload.from_text(truncate_text(clipboard.get_text(), max_text_bytes))


def write_clipboard_payload(clipboard: Clipboard, payload: ClipboardPayload) -> None:
    if payload.kind == "image":
        clipboard.set_image_png(payload.data)
        return
    clipboard.set_text(payload.text)


def clipboard_payload_from_wire(
    payload: dict[str, Any],
    max_text_bytes: int,
    max_image_bytes: int,
) -> ClipboardPayload:
    kind = str(payload.get("kind") or "text")
    if kind == "image":
        mime = str(payload.get("mime") or "image/png").lower()
        if mime != "image/png":
            raise ClipboardError(f"unsupported clipboard image type {mime!r}")
        try:
            data = base64.b64decode(str(payload.get("data_b64") or ""), validate=True)
        except ValueError as exc:
            raise ClipboardError("clipboard image payload is not valid base64") from exc
        if not data:
            raise ClipboardError("clipboard image payload is empty")
        if len(data) > max_image_bytes:
            raise ClipboardError(
                f"clipboard image payload is {len(data)} bytes; limit is {max_image_bytes} bytes"
            )
        return ClipboardPayload.from_image_png(data)
    return ClipboardPayload.from_text(truncate_text(str(payload.get("text", "")), max_text_bytes))


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


def _run_binary_clipboard_command(command: list[str], input_bytes: bytes | None = None) -> bytes:
    try:
        result = subprocess.run(
            command,
            input=input_bytes,
            capture_output=True,
            check=True,
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


def _linux_get_image_png() -> bytes | None:
    for command in (
        ["wl-paste", "--type", "image/png"],
        ["xclip", "-selection", "clipboard", "-target", "image/png", "-out"],
    ):
        if shutil.which(command[0]):
            try:
                data = _run_binary_clipboard_command(command)
            except ClipboardError:
                continue
            return data or None
    return None


def _linux_set_image_png(data: bytes) -> None:
    for command in (
        ["wl-copy", "--type", "image/png"],
        ["xclip", "-selection", "clipboard", "-target", "image/png", "-in"],
    ):
        if shutil.which(command[0]):
            _run_binary_clipboard_command(command, input_bytes=data)
            return
    raise ClipboardError("no supported Linux image clipboard command found")


def _image_content_digest(data: bytes) -> str:
    try:
        image = _open_image(data)
        image.load()
        normalized = image.convert("RGBA")
        hasher = hashlib.sha256()
        hasher.update(str(normalized.width).encode("ascii"))
        hasher.update(b"x")
        hasher.update(str(normalized.height).encode("ascii"))
        hasher.update(b":")
        hasher.update(normalized.tobytes())
        return hasher.hexdigest()
    except ClipboardError:
        return hashlib.sha256(data).hexdigest()


def _image_bytes_to_png(data: bytes) -> bytes:
    image = _open_image(data)
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def _open_image(data: bytes):
    try:
        from PIL import Image
    except ImportError as exc:
        raise ClipboardError("image clipboard support requires Pillow") from exc

    try:
        return Image.open(BytesIO(data))
    except Exception as exc:
        raise ClipboardError("clipboard image could not be decoded") from exc


def _pillow_grab_clipboard_image_png() -> bytes | None:
    try:
        from PIL import Image, ImageGrab
    except ImportError:
        return None

    try:
        value = ImageGrab.grabclipboard()
    except Exception as exc:
        raise ClipboardError("could not read clipboard image") from exc
    if not isinstance(value, Image.Image):
        return None
    output = BytesIO()
    value.save(output, format="PNG")
    return output.getvalue()


def _macos_get_image_png() -> bytes | None:
    try:
        from AppKit import NSPasteboard
    except ImportError:
        return _pillow_grab_clipboard_image_png()

    pasteboard = NSPasteboard.generalPasteboard()
    for paste_type in ("public.png", "NSPasteboardTypePNG"):
        data = pasteboard.dataForType_(paste_type)
        if data:
            return bytes(data)
    for paste_type in ("public.tiff", "NSTIFFPboardType", "public.jpeg", "public.heic"):
        data = pasteboard.dataForType_(paste_type)
        if data:
            try:
                return _image_bytes_to_png(bytes(data))
            except ClipboardError:
                continue
    return _pillow_grab_clipboard_image_png()


def _macos_set_image_png(data: bytes) -> None:
    try:
        from AppKit import NSImage, NSPasteboard
        from Foundation import NSData
    except ImportError as exc:
        raise ClipboardError("macOS image clipboard support requires PyObjC") from exc

    pasteboard = NSPasteboard.generalPasteboard()
    nsdata = NSData.dataWithBytes_length_(data, len(data))
    image = NSImage.alloc().initWithData_(nsdata)
    if image is None:
        raise ClipboardError("could not decode macOS clipboard image")

    pasteboard.clearContents()
    if pasteboard.writeObjects_([image]):
        return

    tiff_data = image.TIFFRepresentation()
    pasteboard.clearContents()
    pasteboard.declareTypes_owner_(["public.png", "public.tiff"], None)
    if pasteboard.setData_forType_(nsdata, "public.png"):
        return
    if tiff_data is not None and pasteboard.setData_forType_(tiff_data, "public.tiff"):
        return
    raise ClipboardError("could not set macOS clipboard image")


def _windows_set_image_png(data: bytes) -> None:
    import ctypes
    from ctypes import wintypes

    dib = _windows_png_to_dib(data)
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    cf_dib = 8
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

    handle = kernel32.GlobalAlloc(gmem_moveable, len(dib))
    if not handle:
        raise ClipboardError("could not allocate Windows clipboard image memory")

    pointer = kernel32.GlobalLock(handle)
    if not pointer:
        kernel32.GlobalFree(handle)
        raise ClipboardError("could not lock Windows clipboard image memory")
    try:
        ctypes.memmove(pointer, dib, len(dib))
    finally:
        kernel32.GlobalUnlock(handle)

    if not user32.OpenClipboard(None):
        kernel32.GlobalFree(handle)
        raise ClipboardError("could not open Windows clipboard")
    try:
        if not user32.EmptyClipboard():
            raise ClipboardError("could not empty Windows clipboard")
        if not user32.SetClipboardData(cf_dib, handle):
            raise ClipboardError("could not set Windows clipboard image")
        handle = None
    finally:
        user32.CloseClipboard()
        if handle:
            kernel32.GlobalFree(handle)


def _windows_png_to_dib(data: bytes) -> bytes:
    image = _open_image(data)
    output = BytesIO()
    image.save(output, format="BMP")
    bmp = output.getvalue()
    if len(bmp) < 14 or bmp[:2] != b"BM":
        raise ClipboardError("could not convert image to Windows DIB")
    return bmp[14:]


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
