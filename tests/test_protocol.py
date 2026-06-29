from __future__ import annotations

import json
import unittest

from mwbc.protocol import AuthenticationError, decode_message, decode_with_any_secret, encode_message


class ProtocolTests(unittest.TestCase):
    def test_round_trip_signed_message(self) -> None:
        encoded = encode_message("input", {"action": "move", "x": 10, "y": 20}, "secret")
        message = decode_message(encoded, "secret")
        self.assertEqual(message.type, "input")
        self.assertEqual(message.payload["x"], 10)
        self.assertEqual(message.payload["y"], 20)

    def test_tampered_payload_fails_authentication(self) -> None:
        encoded = encode_message("input", {"action": "move", "x": 10, "y": 20}, "secret")
        envelope = json.loads(encoded)
        envelope["payload"]["x"] = 11
        tampered = json.dumps(envelope).encode("utf-8")
        with self.assertRaises(AuthenticationError):
            decode_message(tampered, "secret")

    def test_decode_with_any_secret_returns_matching_secret(self) -> None:
        encoded = encode_message("hello", {"machine_name": "desk"}, "right-secret")
        message, secret = decode_with_any_secret(encoded, ["wrong", "right-secret"])
        self.assertEqual(message.type, "hello")
        self.assertEqual(secret, "right-secret")


if __name__ == "__main__":
    unittest.main()

