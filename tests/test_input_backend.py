from __future__ import annotations

import unittest
from types import SimpleNamespace

from mwbc.input_backend import PynputBackend


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


if __name__ == "__main__":
    unittest.main()
