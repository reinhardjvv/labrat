"""Leases: a short, signed permission to talk to a rat's devices directly, without the nest in between.

    lease = lab.lease("optics-rat/slm", minutes=1)       # from the nest, which checks who may have it
    slm = lab.direct("optics-rat/slm")                     # the same, with the fastest path chosen and renewed

The nest grants a lease only to someone who may change the devices and whose changes need nobody's approval. It
locks the devices for them, writes its own limits into the lease and signs it with the rat's key. The rat checks the
signature and the time, applies the nest's limits on top of its own, refuses everyone else's changes to those
devices, and logs what the holder does. Nothing more is needed from the nest until the lease is renewed.

    token  = base64url(JSON body) "." HMAC-SHA256(rat key, body)
    secret = HMAC-SHA256(rat key, "session:" + lease id)

The holder sends the token as 'Authorization: Lease <token>'. The secret, which the rat can work out and the
holder gets from the nest, lets the holder check it reached the real rat (GET /api/v1/health?lease=<id>&challenge=c
answers HMAC(secret, c)) before sending anything to it.

A lease lasts a minute unless it is renewed. When one ends without being released after it changed something, the
rat puts the leased devices in their safe state: a crashed script, a lost network or a nest that stopped answering
leaves the equipment safe.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time

from .message import LabratError

MINUTES_DEFAULT = 1.0
MINUTES_MAX = 10.0


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def sign(key: str, body: dict) -> str:
    data = _b64(json.dumps(body, separators=(",", ":"), sort_keys=True).encode())
    return data + "." + hmac.new(key.encode(), data.encode(), hashlib.sha256).hexdigest()


def secret(key: str, lease_id: str) -> str:
    return hmac.new(key.encode(), f"session:{lease_id}".encode(), hashlib.sha256).hexdigest()


def proof(secret_: str, challenge: str) -> str:
    return hmac.new(secret_.encode(), challenge.encode(), hashlib.sha256).hexdigest()


def verify(key: str, token: str, now: float | None = None) -> dict:
    """The body of a lease signed with this key that has not run out, or LabratError('unauthorised')."""
    data, _, sig = token.partition(".")
    if not data or not hmac.compare_digest(sig, hmac.new(key.encode(), data.encode(), hashlib.sha256).hexdigest()):
        raise LabratError("unauthorised", "the lease is not signed by this rat's nest")
    try:
        body = json.loads(_unb64(data))
    except ValueError:
        raise LabratError("unauthorised", "malformed lease") from None
    if not isinstance(body, dict) or not {"id", "who", "devices", "exp"} <= set(body):
        raise LabratError("unauthorised", "malformed lease")
    if float(body["exp"]) < (now or time.time()):
        raise LabratError("unauthorised", f"lease {body['id']} has run out; renew it at the nest")
    return body
