"""The LABRAT message: one small JSON request and one JSON reply, used by every part of LABRAT.

A request names an operation and, where it needs them, a device, an attribute or function name, a channel,
a value and arguments:

    {"op": "set", "device": "bench-rat/psu", "name": "VoltageLevel", "channel": "1", "value": 3.3}

A reply says whether it worked and carries the value, or an error with a stable code:

    {"ok": true, "value": 3.3, "unit": "V"}
    {"ok": false, "error": {"code": "limit", "message": "VoltageLevel 12 V is above the limit of 5 V"}}

The same message travels over HTTP, through the MCP server and between the nest and its rats.
See docs/api.md for every operation.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# operation -> the least role allowed to send it
OPS: dict[str, str] = {
    "health": "viewer",
    "list": "viewer",
    "describe": "viewer",
    "get": "viewer",
    "set": "operator",
    "call": "operator",
    "lock": "operator",
    "unlock": "operator",
    "stop": "viewer",          # anyone who can see the lab can put it in its safe state
    "rats": "viewer",
    "adopt": "admin",
    "forget": "admin",
}
ROLES = ("viewer", "operator", "admin")

# error code -> HTTP status
CODES: dict[str, int] = {
    "bad_request": 400,
    "unauthorised": 401,
    "forbidden": 403,
    "not_found": 404,
    "locked": 423,
    "limit": 422,
    "device": 502,
    "unavailable": 503,
    "internal": 500,
}


class LabratError(Exception):
    """An error with a stable code from CODES, safe to show to whoever sent the request."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code if code in CODES else "internal"
        self.message = message

    @property
    def status(self) -> int:
        return CODES[self.code]


def role_allows(role: str, op: str) -> bool:
    """True when a principal with this role may send this operation."""
    need = OPS.get(op)
    return need is not None and role in ROLES and ROLES.index(role) >= ROLES.index(need)


@dataclass
class Message:
    op: str
    device: str | None = None
    name: str | None = None
    channel: str | None = None
    value: Any = None
    args: dict = field(default_factory=dict)
    id: Any = None

    def to_dict(self) -> dict:
        d = {"op": self.op}
        for k in ("device", "name", "channel", "value", "id"):
            v = getattr(self, k)
            if v is not None:
                d[k] = v
        if self.args:
            d["args"] = self.args
        return d


NEEDS = {
    "describe": ("device",),
    "get": ("device", "name"),
    "set": ("device", "name", "value"),
    "call": ("device", "name"),
    "lock": ("device",),
    "unlock": ("device",),
    "adopt": ("name",),
    "forget": ("name",),
}


def parse(obj: Any) -> Message:
    """Check a decoded JSON request and return it as a Message, or raise LabratError('bad_request')."""
    if not isinstance(obj, dict):
        raise LabratError("bad_request", "a message is a JSON object")
    op = obj.get("op")
    if op not in OPS:
        raise LabratError("bad_request", f"unknown op {op!r}; one of {', '.join(OPS)}")
    unknown = set(obj) - {"op", "device", "name", "channel", "value", "args", "id"}
    if unknown:
        raise LabratError("bad_request", f"unknown field(s) {', '.join(sorted(unknown))}")
    for k in ("device", "name", "channel"):
        if obj.get(k) is not None and not isinstance(obj[k], (str, int)):
            raise LabratError("bad_request", f"{k} must be a string")
    args = obj.get("args") or {}
    if not isinstance(args, dict):
        raise LabratError("bad_request", "args must be an object")
    m = Message(op=op, device=_s(obj.get("device")), name=_s(obj.get("name")), channel=_s(obj.get("channel")),
                value=obj.get("value"), args=args, id=obj.get("id"))
    for k in NEEDS.get(op, ()):
        if getattr(m, k) is None:
            raise LabratError("bad_request", f"{op} needs {k}")
    return m


def _s(v):
    return None if v is None else str(v)


def ok(value: Any = None, msg_id: Any = None, **extra) -> dict:
    reply = {"ok": True}
    if value is not None:
        reply["value"] = value
    reply.update({k: v for k, v in extra.items() if v is not None})
    if msg_id is not None:
        reply["id"] = msg_id
    return reply


def fail(err: LabratError, msg_id: Any = None) -> dict:
    reply = {"ok": False, "error": {"code": err.code, "message": err.message}}
    if msg_id is not None:
        reply["id"] = msg_id
    return reply
