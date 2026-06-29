from __future__ import annotations

import logging
import sys
from dataclasses import dataclass
from typing import Any, Callable, Protocol

logger = logging.getLogger(__name__)


MouseMoveCallback = Callable[[int, int], None]
MouseClickCallback = Callable[[int, int, str, bool], None]
MouseScrollCallback = Callable[[int, int, int, int], None]
KeyCallback = Callable[[dict[str, str]], None]


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

    def start_capture(self, callbacks: CaptureCallbacks, *, suppress: bool) -> None:
        ...

    def stop_capture(self) -> None:
        ...


class NullBackend:
    name = "null"

    def __init__(self, reason: str | None = None) -> None:
        self.reason = reason
        self._position = (960, 540)

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

    def screen_size(self) -> tuple[int, int]:
        try:
            import tkinter as tk

            root = tk.Tk()
            root.withdraw()
            width = int(root.winfo_screenwidth())
            height = int(root.winfo_screenheight())
            root.destroy()
            return (width, height)
        except Exception:
            if sys.platform.startswith("win"):
                try:
                    import ctypes

                    user32 = ctypes.windll.user32
                    return (int(user32.GetSystemMetrics(0)), int(user32.GetSystemMetrics(1)))
                except Exception:
                    pass
            return (1920, 1080)

    def current_position(self) -> tuple[int, int]:
        x, y = self._mouse_controller.position
        return (int(x), int(y))

    def move_to(self, x: int, y: int) -> None:
        self._mouse_controller.position = (int(x), int(y))

    def move_relative(self, dx: int, dy: int) -> None:
        self._mouse_controller.move(int(dx), int(dy))

    def click(self, button: str, pressed: bool) -> None:
        btn = self._button_from_name(button)
        if pressed:
            self._mouse_controller.press(btn)
        else:
            self._mouse_controller.release(btn)

    def scroll(self, dx: int, dy: int) -> None:
        self._mouse_controller.scroll(int(dx), int(dy))

    def key_press(self, key: dict[str, str]) -> None:
        self._keyboard_controller.press(self._key_from_wire(key))

    def key_release(self, key: dict[str, str]) -> None:
        self._keyboard_controller.release(self._key_from_wire(key))

    def start_capture(self, callbacks: CaptureCallbacks, *, suppress: bool) -> None:
        self.stop_capture()

        def on_move(x: int, y: int) -> None:
            callbacks.on_move(int(x), int(y))

        def on_click(x: int, y: int, button: Any, pressed: bool) -> None:
            callbacks.on_click(int(x), int(y), self._button_to_name(button), bool(pressed))

        def on_scroll(x: int, y: int, dx: int, dy: int) -> None:
            callbacks.on_scroll(int(x), int(y), int(dx), int(dy))

        def on_press(key: Any) -> None:
            callbacks.on_key_press(self._key_to_wire(key))

        def on_release(key: Any) -> None:
            callbacks.on_key_release(self._key_to_wire(key))

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

    def _key_to_wire(self, key: Any) -> dict[str, str]:
        char = getattr(key, "char", None)
        if char:
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
