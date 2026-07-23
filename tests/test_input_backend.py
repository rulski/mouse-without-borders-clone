from __future__ import annotations

import threading
import unittest
from types import SimpleNamespace

from mwbc.input_backend import PynputBackend


class FakeKey:
    shift = "SHIFT"
    shift_l = "SHIFT_L"
    shift_r = "SHIFT_R"


class FakeKeyboardModule:
    Key = FakeKey


class FakeKeyboardController:
    def __init__(self) -> None:
        self.events: list[tuple[str, str]] = []

    def press(self, key: str) -> None:
        self.events.append(("press", key))

    def release(self, key: str) -> None:
        self.events.append(("release", key))


class FakeButton:
    left = "LEFT"
    right = "RIGHT"
    middle = "MIDDLE"


class FakeMouseModule:
    Button = FakeButton


class FakeMouseController:
    def __init__(self) -> None:
        self.events: list[tuple[str, object]] = []
        self._position = (0, 0)

    @property
    def position(self) -> tuple[int, int]:
        return self._position

    @position.setter
    def position(self, value: tuple[int, int]) -> None:
        self._position = value
        self.events.append(("move_to", value))

    def move(self, dx: int, dy: int) -> None:
        self.events.append(("move_relative", (dx, dy)))

    def click(self, button: str, count: int) -> None:
        self.events.append(("click", (button, count)))

    def press(self, button: str) -> None:
        self.events.append(("press", button))

    def release(self, button: str) -> None:
        self.events.append(("release", button))

    def scroll(self, dx: int, dy: int) -> None:
        self.events.append(("scroll", (dx, dy)))


class FakeTimer:
    def __init__(self) -> None:
        self.started = False
        self.canceled = False

    def start(self) -> None:
        self.started = True

    def cancel(self) -> None:
        self.canceled = True


def make_replay_backend() -> tuple[PynputBackend, FakeKeyboardController]:
    backend = object.__new__(PynputBackend)
    controller = FakeKeyboardController()
    backend._keyboard_module = FakeKeyboardModule
    backend._keyboard_controller = controller
    backend._replay_shift_keys = set()
    backend._replay_one_shot_chars = set()
    return backend, controller


def make_mouse_backend(*, coalesce: bool = False) -> tuple[PynputBackend, FakeMouseController]:
    backend = object.__new__(PynputBackend)
    controller = FakeMouseController()
    backend._mouse_module = FakeMouseModule
    backend._mouse_controller = controller
    backend._pending_click_presses = {}
    backend._pending_tap_clicks = {}
    backend._click_lock = threading.RLock()
    backend._coalesce_tap_clicks = coalesce
    backend._fake_timers = []

    def schedule_tap_click_flush(button_name: str) -> FakeTimer:
        timer = FakeTimer()
        timer.start()
        backend._fake_timers.append((button_name, timer))
        return timer

    backend._schedule_tap_click_flush = schedule_tap_click_flush  # type: ignore[method-assign]
    return backend, controller


class InputBackendTests(unittest.TestCase):
    def test_shifted_number_symbols_are_sent_as_base_keys_when_shift_is_down(self) -> None:
        backend = object.__new__(PynputBackend)
        backend._shift_down = True

        self.assertEqual(backend._key_to_wire(SimpleNamespace(char="!")), {"kind": "char", "value": "1"})
        self.assertEqual(backend._key_to_wire(SimpleNamespace(char="@")), {"kind": "char", "value": "2"})
        self.assertEqual(backend._key_to_wire(SimpleNamespace(char="?")), {"kind": "char", "value": "/"})

    def test_shifted_letters_are_left_unchanged(self) -> None:
        backend = object.__new__(PynputBackend)
        backend._shift_down = True

        self.assertEqual(backend._key_to_wire(SimpleNamespace(char="A")), {"kind": "char", "value": "A"})

    def test_symbols_are_left_unchanged_when_shift_is_not_down(self) -> None:
        backend = object.__new__(PynputBackend)
        backend._shift_down = False

        self.assertEqual(backend._key_to_wire(SimpleNamespace(char="!")), {"kind": "char", "value": "!"})

    def test_shifted_number_replay_taps_symbol(self) -> None:
        backend, controller = make_replay_backend()

        backend.key_press({"kind": "special", "value": "shift"})
        backend.key_press({"kind": "char", "value": "1"})
        backend.key_release({"kind": "char", "value": "1"})
        backend.key_release({"kind": "special", "value": "shift"})

        self.assertEqual(
            controller.events,
            [
                ("press", "SHIFT"),
                ("release", "SHIFT"),
                ("press", "!"),
                ("release", "!"),
                ("press", "SHIFT"),
                ("release", "SHIFT"),
            ],
        )

    def test_unshifted_number_replay_keeps_base_key(self) -> None:
        backend, controller = make_replay_backend()

        backend.key_press({"kind": "char", "value": "1"})
        backend.key_release({"kind": "char", "value": "1"})

        self.assertEqual(controller.events, [("press", "1"), ("release", "1")])

    def test_shifted_letter_replay_is_left_unchanged(self) -> None:
        backend, controller = make_replay_backend()

        backend.key_press({"kind": "special", "value": "shift"})
        backend.key_press({"kind": "char", "value": "A"})
        backend.key_release({"kind": "char", "value": "A"})
        backend.key_release({"kind": "special", "value": "shift"})

        self.assertEqual(
            controller.events,
            [("press", "SHIFT"), ("press", "A"), ("release", "A"), ("release", "SHIFT")],
        )

    def test_simple_mouse_click_replays_as_native_click(self) -> None:
        backend, controller = make_mouse_backend()

        backend.click("left", True)
        backend.click("left", False)

        self.assertEqual(controller.events, [("press", "LEFT"), ("release", "LEFT")])

    def test_mouse_drag_flushes_pending_press_before_move(self) -> None:
        backend, controller = make_mouse_backend()

        backend.click("left", True)
        backend.move_relative(4, 5)
        backend.click("left", False)

        self.assertEqual(
            controller.events,
            [("press", "LEFT"), ("move_relative", (4, 5)), ("release", "LEFT")],
        )

    def test_coalesced_single_click_waits_until_flush(self) -> None:
        backend, controller = make_mouse_backend(coalesce=True)

        backend.click("left", True)
        backend.click("left", False)

        self.assertEqual(controller.events, [])
        self.assertEqual(len(backend._pending_tap_clicks), 1)
        backend._flush_pending_tap_clicks()
        self.assertEqual(controller.events, [("click", ("LEFT", 1))])
        self.assertTrue(backend._fake_timers[0][1].canceled)

    def test_coalesced_double_click_replays_as_native_double_click(self) -> None:
        backend, controller = make_mouse_backend(coalesce=True)

        backend.click("left", True)
        backend.click("left", False)
        backend.click("left", True)
        backend.click("left", False)

        self.assertEqual(controller.events, [("click", ("LEFT", 2))])
        self.assertEqual(backend._pending_tap_clicks, {})
        self.assertTrue(backend._fake_timers[0][1].canceled)

    def test_coalesced_click_flushes_before_move(self) -> None:
        backend, controller = make_mouse_backend(coalesce=True)

        backend.click("left", True)
        backend.click("left", False)
        backend.move_relative(4, 5)

        self.assertEqual(controller.events, [("click", ("LEFT", 1)), ("move_relative", (4, 5))])

    def test_coalesced_drag_flushes_pending_press_before_move(self) -> None:
        backend, controller = make_mouse_backend(coalesce=True)

        backend.click("left", True)
        backend.move_relative(4, 5)
        backend.click("left", False)

        self.assertEqual(
            controller.events,
            [("press", "LEFT"), ("move_relative", (4, 5)), ("release", "LEFT")],
        )


if __name__ == "__main__":
    unittest.main()
