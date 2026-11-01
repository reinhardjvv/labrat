"""Signed requests between a nest and its workers.

A worker shares a key with its nest from the moment it joins. Every later request carries an HMAC of the time, a
fresh nonce and the body instead of the key, and every reply an HMAC of that nonce and the reply's body:

    Authorization: Worker <name>:<unix time>:<nonce>:<hmac-sha256(key, time \\n nonce \\n sha256(body))>
    X-Labrat-Signature: <hmac-sha256(key, nonce \\n sha256(reply))>

The key therefore never travels after joining, a recorded request cannot be sent again, and a worker runs only
jobs its own nest signed. Plain HTTP still lets an eavesdropper read what passes; use TLS or a VPN for computers
outside the lab network (docs/workers.md).
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
import time

from .message import LabratError

WINDOW_S = 300                      # how far a request's time may be from the nest's clock


def digest(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def mac(key: str, *parts: str) -> str:
    return hmac.new(key.encode(), "\n".join(parts).encode(), hashlib.sha256).hexdigest()


def request_header(name: str, key: str, body: bytes) -> tuple[str, str]:
    """The Authorization header for a request, and the nonce its reply must be signed with."""
    ts, nonce = str(int(time.time())), secrets.token_hex(12)
    return f"Worker {name}:{ts}:{nonce}:{mac(key, ts, nonce, digest(body))}", nonce


def parse_header(header: str | None) -> tuple[str, int, str, str]:
    """(name, time, nonce, signature) from an Authorization header, or LabratError('unauthorised')."""
    if not header or not header.startswith("Worker "):
        raise LabratError("unauthorised", "a worker signs its requests (Authorization: Worker ...)")
    parts = header[7:].strip().split(":")
    if len(parts) != 4 or not parts[1].isdigit() or not 8 <= len(parts[2]) <= 64:
        raise LabratError("unauthorised", "malformed worker signature")
    return parts[0], int(parts[1]), parts[2], parts[3]


def check_request(key: str, ts: int, nonce: str, signature: str, body: bytes) -> None:
    if abs(time.time() - ts) > WINDOW_S:
        raise LabratError("unauthorised", f"the request's time is more than {WINDOW_S} s from the nest's clock; "
                          "set the worker's clock (NTP)")
    if not hmac.compare_digest(signature, mac(key, str(ts), nonce, digest(body))):
        raise LabratError("unauthorised", "the worker signature does not match")


def reply_signature(key: str, nonce: str, body: bytes) -> str:
    return mac(key, nonce, digest(body))


class Nonces:
    """Nonces seen in the last two windows, so the same signed request is accepted once."""

    def __init__(self):
        self._seen: dict[str, float] = {}
        self._lock = threading.Lock()

    def fresh(self, nonce: str) -> bool:
        now = time.monotonic()
        with self._lock:
            if len(self._seen) > 10000 or (self._seen and min(self._seen.values()) < now - 2 * WINDOW_S):
                self._seen = {n: t for n, t in self._seen.items() if t > now - 2 * WINDOW_S}
            if nonce in self._seen:
                return False
            self._seen[nonce] = now
            return True
