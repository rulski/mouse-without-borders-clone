from __future__ import annotations

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


def make_replay_backend() -> tuple[PynputBackend, FakeKeyboardController]:
    backend = object.__new__(PynputBackend)
    controller = FakeKeyboardController()
    backend._keyboard_module = FakeKeyboardModule
    backend._keyboard_controller = controller
    backend._replay_shift_keys = set()
    backend._replay_one_shot_chars = set()
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


if __name__ == "__main__":
    unittest.main()
