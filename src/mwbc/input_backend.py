from __future__ import annotations

import logging
import sys
import threading
from dataclasses import dataclass
from typing import Any, Callable, Protocol

logger = logging.getLogger(__name__)


MouseMoveCallback = Callable[[int, int], None]
MouseClickCallback = Callable[[int, int, str, bool], None]
MouseScrollCallback = Callable[[int, int, int, int], None]
KeyCallback = Callable[[dict[str, str]], None]
DOUBLE_CLICK_WINDOW_SECONDS = 0.45


SHIFTED_SYMBOL_TO_BASE = {
    "~": "`",
    "!": "1",
    "@": "2",
    "#": "3",
    "$": "4",
    "%": "5",
    "^": "6",
    "&": "7",
    "*": "8",
    "(": "9",
    ")": "0",
    "_": "-",
    "+": "=",
    "{": "[",
    "}": "]",
    "|": "\\",
    ":": ";",
    '"': "'",
    "<": ",",
    ">": ".",
    "?": "/",
}
BASE_TO_SHIFTED_SYMBOL = {base: shifted for shifted, base in SHIFTED_SYMBOL_TO_BASE.items()}


@dataclass(slots=True)
class PendingTapClick:
    button: Any
    timer: threading.Timer


@dataclass(frozen=True, slots=True)
class CaptureCallbacks:
    on_move: MouseMoveCallback
    on_click: MouseClickCallback
    on_scroll: MouseScrollCallback
    on_key_press: KeyCallback
    on_key_release: KeyCallback


class InputBackend(Protocol):
    name: str

    def screen_size(self) -> tuple[int, int]:
        ...

    def current_position(self) -> tuple[int, int]:
        ...

    def move_to(self, x: int, y: int) -> None:
        ...

    def move_relative(self, dx: int, dy: int) -> None:
        ...

    def click(self, button: str, pressed: bool) -> None:
        ...

    def scroll(self, dx: int, dy: int) -> None:
        ...

    def key_press(self, key: dict[str, str]) -> None:
        ...

    def key_release(self, key: dict[str, str]) -> None:
        ...

    def set_cursor_visible(self, visible: bool) -> None:
        ...

    def start_capture(self, callbacks: CaptureCallbacks, *, suppress: bool) -> None:
        ...

    def stop_capture(self) -> None:
        ...


class NullBackend:
    name = "null"

    def __init__(self, reason: str | None = None) -> None:
        self.reason = reason
        self._position = (960, 540)
        self._cursor_visible = True

    def screen_size(self) -> tuple[int, int]:
        return (1920, 1080)

    def current_position(self) -> tuple[int, int]:
        return self._position

    def move_to(self, x: int, y: int) -> None:
        self._position = (int(x), int(y))
        logger.info("null move_to(%s, %s)", x, y)

    def move_relative(self, dx: int, dy: int) -> None:
        x, y = self._position
        self._position = (x + int(dx), y + int(dy))
        logger.info("null move_relative(%s, %s)", dx, dy)

    def click(self, button: str, pressed: bool) -> None:
        logger.info("null click(%s, pressed=%s)", button, pressed)

    def scroll(self, dx: int, dy: int) -> None:
        logger.info("null scroll(%s, %s)", dx, dy)

    def key_press(self, key: dict[str, str]) -> None:
        logger.info("null key_press(%s)", key)

    def key_release(self, key: dict[str, str]) -> None:
        logger.info("null key_release(%s)", key)

    def set_cursor_visible(self, visible: bool) -> None:
        self._cursor_visible = bool(visible)
        logger.info("null set_cursor_visible(%s)", visible)

    def start_capture(self, callbacks: CaptureCallbacks, *, suppress: bool) -> None:
        logger.warning("null backend cannot capture native input%s", f": {self.reason}" if self.reason else "")

    def stop_capture(self) -> None:
        return None


class PynputBackend:
    name = "pynput"

    def __init__(self) -> None:
        from pynput import keyboard, mouse

        self._keyboard_module = keyboard
        self._mouse_module = mouse
        self._mouse_controller = mouse.Controller()
        self._keyboard_controller = keyboard.Controller()
        self._mouse_listener: Any = None
        self._keyboard_listener: Any = None
        self._shift_down = False
        self._replay_shift_keys: set[str] = set()
        self._replay_one_shot_chars: set[str] = set()
        self._pending_click_presses: dict[str, Any] = {}
        self._pending_tap_clicks: dict[str, PendingTapClick] = {}
        self._click_lock = threading.RLock()
        self._coalesce_tap_clicks = sys.platform == "darwin"
        self._cursor_hidden = False

    def screen_size(self) -> tuple[int, int]:
        if sys.platform.startswith("win"):
            try:
                import ctypes

                user32 = ctypes.windll.user32
                return (int(user32.GetSystemMetrics(0)), int(user32.GetSystemMetrics(1)))
            except Exception:
                pass

        try:
            import tkinter as tk

            root = tk.Tk()
            root.withdraw()
            width = int(root.winfo_screenwidth())
            height = int(root.winfo_screenheight())
            root.destroy()
            return (width, height)
        except Exception:
            return (1920, 1080)

    def current_position(self) -> tuple[int, int]:
        x, y = self._mouse_controller.position
        return (int(x), int(y))

    def move_to(self, x: int, y: int) -> None:
        self._flush_pending_tap_clicks()
        self._flush_pending_click_presses()
        self._mouse_controller.position = (int(x), int(y))

    def move_relative(self, dx: int, dy: int) -> None:
        self._flush_pending_tap_clicks()
        self._flush_pending_click_presses()
        self._mouse_controller.move(int(dx), int(dy))

    def click(self, button: str, pressed: bool) -> None:
        btn = self._button_from_name(button)
        if not self._coalesce_tap_clicks:
            if pressed:
                self._mouse_controller.press(btn)
            else:
                self._mouse_controller.release(btn)
            return

        if pressed:
            self._pending_click_presses.setdefault(button, btn)
        else:
            pending = self._pending_click_presses.pop(button, None)
            if pending is not None:
                self._queue_tap_click(button, pending)
            else:
                self._mouse_controller.release(btn)

    def scroll(self, dx: int, dy: int) -> None:
        self._flush_pending_tap_clicks()
        self._flush_pending_click_presses()
        self._mouse_controller.scroll(int(dx), int(dy))

    def key_press(self, key: dict[str, str]) -> None:
        if _is_shift_wire(key):
            self._replay_shift_keys.add(_shift_wire_value(key))
            self._keyboard_controller.press(self._key_from_wire(key))
            return

        one_shot = self._shifted_symbol_for_replay(key)
        if one_shot is not None:
            self._replay_one_shot_chars.add(str(key.get("value", "")))
            self._tap_text_key(one_shot)
            return

        self._keyboard_controller.press(self._key_from_wire(key))

    def key_release(self, key: dict[str, str]) -> None:
        if _is_shift_wire(key):
            try:
                self._keyboard_controller.release(self._key_from_wire(key))
            finally:
                self._replay_shift_keys.discard(_shift_wire_value(key))
            return

        value = str(key.get("value", ""))
        if key.get("kind") == "char" and value in self._replay_one_shot_chars:
            self._replay_one_shot_chars.discard(value)
            return

        self._keyboard_controller.release(self._key_from_wire(key))

    def set_cursor_visible(self, visible: bool) -> None:
        if visible and self._cursor_hidden:
            self._show_cursor()
            self._cursor_hidden = False
        elif not visible and not self._cursor_hidden:
            self._hide_cursor()
            self._cursor_hidden = True

    def start_capture(self, callbacks: CaptureCallbacks, *, suppress: bool) -> None:
        self.stop_capture()

        def on_move(x: int, y: int) -> None:
            callbacks.on_move(int(x), int(y))

        def on_click(x: int, y: int, button: Any, pressed: bool) -> None:
            callbacks.on_click(int(x), int(y), self._button_to_name(button), bool(pressed))

        def on_scroll(x: int, y: int, dx: int, dy: int) -> None:
            callbacks.on_scroll(int(x), int(y), int(dx), int(dy))

        def on_press(key: Any) -> None:
            wire = self._key_to_wire(key)
            if _is_shift_wire(wire):
                self._shift_down = True
            callbacks.on_key_press(wire)

        def on_release(key: Any) -> None:
            wire = self._key_to_wire(key)
            callbacks.on_key_release(wire)
            if _is_shift_wire(wire):
                self._shift_down = False

        self._mouse_listener = self._mouse_module.Listener(
            on_move=on_move,
            on_click=on_click,
            on_scroll=on_scroll,
            suppress=suppress,
        )
        self._keyboard_listener = self._keyboard_module.Listener(
            on_press=on_press,
            on_release=on_release,
            suppress=suppress,
        )
        self._mouse_listener.start()
        self._keyboard_listener.start()

    def stop_capture(self) -> None:
        for listener_name in ("_mouse_listener", "_keyboard_listener"):
            listener = getattr(self, listener_name, None)
            if listener is not None:
                try:
                    listener.stop()
                except Exception:
                    logger.debug("failed to stop %s", listener_name, exc_info=True)
                setattr(self, listener_name, None)

    def _button_to_name(self, button: Any) -> str:
        name = str(button)
        if "." in name:
            return name.rsplit(".", 1)[-1]
        return name

    def _button_from_name(self, name: str) -> Any:
        return getattr(self._mouse_module.Button, name, self._mouse_module.Button.left)

    def _flush_pending_click_presses(self) -> None:
        if not self._pending_click_presses:
            return
        pending = list(self._pending_click_presses.values())
        self._pending_click_presses.clear()
        for button in pending:
            self._mouse_controller.press(button)

    def _queue_tap_click(self, button_name: str, button: Any) -> None:
        with self._click_lock:
            prior = self._pending_tap_clicks.pop(button_name, None)
            if prior is not None:
                prior.timer.cancel()
            else:
                timer = self._schedule_tap_click_flush(button_name)
                self._pending_tap_clicks[button_name] = PendingTapClick(button=button, timer=timer)
                return

        self._mouse_controller.click(button, 2)

    def _schedule_tap_click_flush(self, button_name: str) -> threading.Timer:
        timer = threading.Timer(DOUBLE_CLICK_WINDOW_SECONDS, self._flush_pending_tap_click, args=(button_name,))
        timer.daemon = True
        timer.start()
        return timer

    def _flush_pending_tap_click(self, button_name: str) -> None:
        with self._click_lock:
            pending = self._pending_tap_clicks.pop(button_name, None)
        if pending is not None:
            self._mouse_controller.click(pending.button, 1)

    def _flush_pending_tap_clicks(self) -> None:
        with self._click_lock:
            pending = list(self._pending_tap_clicks.values())
            self._pending_tap_clicks.clear()
        for item in pending:
            item.timer.cancel()
            self._mouse_controller.click(item.button, 1)

    def _key_to_wire(self, key: Any) -> dict[str, str]:
        char = getattr(key, "char", None)
        if char:
            if self._shift_down:
                char = SHIFTED_SYMBOL_TO_BASE.get(char, char)
            return {"kind": "char", "value": char}
        name = str(key)
        if name.startswith("Key."):
            name = name.split(".", 1)[1]
        return {"kind": "special", "value": name}

    def _key_from_wire(self, key: dict[str, str]) -> Any:
        if key.get("kind") == "char":
            return key.get("value", "")
        value = key.get("value", "")
        return getattr(self._keyboard_module.Key, value, value)

    def _shifted_symbol_for_replay(self, key: dict[str, str]) -> str | None:
        if key.get("kind") != "char" or not self._replay_shift_keys:
            return None
        return BASE_TO_SHIFTED_SYMBOL.get(str(key.get("value", "")))

    def _tap_text_key(self, char: str) -> None:
        released_shifts = [self._key_from_wire({"kind": "special", "value": value}) for value in self._replay_shift_keys]
        for shift_key in released_shifts:
            self._keyboard_controller.release(shift_key)
        self._keyboard_controller.press(char)
        self._keyboard_controller.release(char)
        for shift_key in released_shifts:
            self._keyboard_controller.press(shift_key)

    def _hide_cursor(self) -> None:
        if sys.platform.startswith("win"):
            self._set_windows_cursor_visible(False)
            return
        if sys.platform == "darwin":
            self._set_macos_cursor_visible(False)

    def _show_cursor(self) -> None:
        if sys.platform.startswith("win"):
            self._set_windows_cursor_visible(True)
            return
        if sys.platform == "darwin":
            self._set_macos_cursor_visible(True)

    def _set_windows_cursor_visible(self, visible: bool) -> None:
        try:
            import ctypes

            show_cursor = ctypes.windll.user32.ShowCursor
            if visible:
                for _ in range(32):
                    if show_cursor(True) >= 0:
                        break
            else:
                for _ in range(32):
                    if show_cursor(False) < 0:
                        break
        except Exception:
            logger.debug("failed to set Windows cursor visibility", exc_info=True)

    def _set_macos_cursor_visible(self, visible: bool) -> None:
        try:
            from Quartz import CGDisplayHideCursor, CGDisplayShowCursor, CGMainDisplayID

            display_id = CGMainDisplayID()
            if visible:
                CGDisplayShowCursor(display_id)
            else:
                CGDisplayHideCursor(display_id)
        except Exception:
            logger.debug("failed to set macOS cursor visibility", exc_info=True)


def _is_shift_wire(key: dict[str, str]) -> bool:
    if key.get("kind") != "special":
        return False
    return str(key.get("value", "")).lower() in {"shift", "shift_l", "shift_r"}


def _shift_wire_value(key: dict[str, str]) -> str:
    value = str(key.get("value", "shift")).lower()
    return value or "shift"


def create_backend(kind: str) -> InputBackend:
    normalized = kind.lower().strip()
    if normalized == "null":
        return NullBackend()
    if normalized not in {"auto", "pynput"}:
        raise ValueError(f"unknown backend {kind!r}; expected auto, pynput, or null")
    try:
        return PynputBackend()
    except Exception as exc:
        if normalized == "pynput":
            raise
        return NullBackend(reason=str(exc))


def apply_input_event(backend: InputBackend, event: dict[str, Any]) -> None:
    action = event.get("action")
    if action == "move":
        backend.move_to(int(event["x"]), int(event["y"]))
    elif action == "move_relative":
        backend.move_relative(int(event.get("dx", 0)), int(event.get("dy", 0)))
    elif action == "click":
        backend.click(str(event["button"]), bool(event["pressed"]))
    elif action == "scroll":
        backend.scroll(int(event.get("dx", 0)), int(event.get("dy", 0)))
    elif action == "key_press":
        backend.key_press(dict(event["key"]))
    elif action == "key_release":
        backend.key_release(dict(event["key"]))
    else:
        raise ValueError(f"unknown input action {action!r}")
