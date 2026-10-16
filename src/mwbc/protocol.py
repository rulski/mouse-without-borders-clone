from __future__ import annotations

import hashlib
import hmac
import json
import math
import secrets
import time
from dataclasses import dataclass
from typing import Any


class ProtocolError(ValueError):
    """Raised when a protocol frame is malformed."""


class AuthenticationError(ProtocolError):
    """Raised when a protocol frame fails authentication."""


@dataclass(frozen=True, slots=True)
class Message:
    type: str
    payload: dict[str, Any]
    timestamp: float
    nonce: str
    sequence: int | None = None


def _canonical_frame(message_type: str, payload: dict[str, Any], timestamp: float, nonce: str,
                     sequence: int | None = None) -> bytes:
    frame = {
        "type": message_type,
        "payload": payload,
        "timestamp": timestamp,
        "nonce": nonce,
    }
    if sequence is not None:
        frame["sequence"] = sequence
    return json.dumps(frame, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _signature(secret: str, canonical_frame: bytes) -> str:
    return hmac.new(secret.encode("utf-8"), canonical_frame, hashlib.sha256).hexdigest()


def encode_message(message_type: str, payload: dict[str, Any], secret: str, *, sequence: int | None = None) -> bytes:
    timestamp = time.time()
    nonce = secrets.token_urlsafe(18)
    canonical = _canonical_frame(message_type, payload, timestamp, nonce, sequence)
    envelope = {
        "type": message_type,
        "payload": payload,
        "timestamp": timestamp,
        "nonce": nonce,
        "signature": _signature(secret, canonical),
    }
    if sequence is not None:
        envelope["sequence"] = sequence
    return (json.dumps(envelope, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def decode_message(line: bytes | str, secret: str, *, max_age_seconds: int = 300) -> Message:
    if isinstance(line, bytes):
        raw = line.decode("utf-8")
    else:
        raw = line

    try:
        envelope = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ProtocolError("invalid JSON frame") from exc

    try:
        message_type = str(envelope["type"])
        payload = envelope["payload"]
        timestamp = float(envelope["timestamp"])
        nonce = str(envelope["nonce"])
        signature = str(envelope["signature"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ProtocolError("missing or invalid frame field") from exc

    if not isinstance(payload, dict):
        raise ProtocolError("payload must be an object")

    sequence = envelope.get("sequence")
    if sequence is not None and (type(sequence) is not int or sequence < 1):
        raise ProtocolError("sequence must be a positive integer")
    if not math.isfinite(timestamp):
        raise AuthenticationError("invalid frame timestamp")
    if max_age_seconds > 0 and abs(time.time() - timestamp) > max_age_seconds:
        raise AuthenticationError("frame is outside the accepted clock window")

    expected = _signature(secret, _canonical_frame(message_type, payload, timestamp, nonce, sequence))
    if not hmac.compare_digest(signature, expected):
        raise AuthenticationError("frame signature does not match")

    return Message(type=message_type, payload=payload, timestamp=timestamp, nonce=nonce, sequence=sequence)


def session_secret(secret: str, challenge: str) -> str:
    return hmac.new(secret.encode("utf-8"), ("mwbc-session-v1:" + challenge).encode("ascii"), hashlib.sha256).hexdigest()


def decode_with_any_secret(
    line: bytes | str,
    secrets_to_try: list[str],
    *,
    max_age_seconds: int = 300,
) -> tuple[Message, str]:
    auth_error: AuthenticationError | None = None
    for secret in secrets_to_try:
        try:
            return decode_message(line, secret, max_age_seconds=max_age_seconds), secret
        except AuthenticationError as exc:
            auth_error = exc
    if auth_error:
        raise auth_error
    raise AuthenticationError("no shared secrets are configured")

