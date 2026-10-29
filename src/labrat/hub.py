"""The core shared by the nest and the rats: devices, who may do what, locks, the stop and the audit log."""
from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import __version__
from .device import Device, convert
from .message import LabratError, Message, fail, ok, parse, role_allows

log = logging.getLogger("labrat")
SECRET_ARGS = ("key", "password", "token", "secret")


@dataclass(frozen=True)
class Principal:
    name: str
    role: str


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class Hub:
    role = "hub"

    def __init__(self, name: str, devices: dict[str, Device] | None = None, tokens: dict[str, Principal] | None = None,
                 audit_path: Path | None = None, lock_minutes: float = 10.0):
        self.name = name
        self.devices: dict[str, Device] = dict(devices or {})
        self.tokens: dict[str, Principal] = dict(tokens or {})       # sha256 of the token -> who it is
        self.audit_path = audit_path
        self.lock_minutes = lock_minutes
        self.locks: dict[str, tuple[str, float, float]] = {}          # device -> (holder, expiry on time.monotonic, minutes)
        self._state = threading.Lock()
        self._audit = threading.Lock()
        self.started = time.time()

    # -- who is asking --------------------------------------------------------------------------------------
    def authenticate(self, header: str | None) -> Principal:
        if not header or not header.startswith("Bearer "):
            raise LabratError("unauthorised", "send Authorization: Bearer <token>")
        who = self.tokens.get(token_hash(header[7:].strip()))
        if who is None:
            raise LabratError("unauthorised", "unknown token")
        return who

    # -- handling messages ----------------------------------------------------------------------------------
    def handle(self, obj: Any, who: Principal) -> dict:
        msg_id = obj.get("id") if isinstance(obj, dict) else None
        try:
            msg = parse(obj)
            if not role_allows(who.role, msg.op):
                raise LabratError("forbidden", f"{who.name} ({who.role}) may not {msg.op}")
            reply = self.dispatch(msg, who)
            if msg.op in ("set", "call", "lock", "unlock", "stop", "adopt", "forget"):
                self.audit(who, msg, True)
            reply.setdefault("id", msg_id) if msg_id is not None else None
            return reply
        except LabratError as e:
            if isinstance(obj, dict) and obj.get("op") in ("set", "call", "lock", "unlock", "stop", "adopt", "forget"):
                self.audit(who, obj, False, e)
            return fail(e, msg_id)
        except Exception:                                   # a driver bug must not take the server down
            op = obj.get("op") if isinstance(obj, dict) else None
            log.exception("unexpected error handling %s for %s", op, who.name)     # never the message: it may hold a key
            return fail(LabratError("internal", "unexpected error; see the server log"), msg_id)

    def dispatch(self, m: Message, who: Principal) -> dict:
        if m.op == "health":
            return ok(self.health())
        if m.op == "list":
            return ok(self.list_devices())
        if m.op == "stop":
            return ok(self.stop(who, str(m.args.get("reason", ""))))
        if m.op in ("rats", "adopt", "forget"):
            raise LabratError("not_found", f"{m.op} is a nest operation; this is a {self.role}")
        if m.op == "lock":
            return ok(self.lock(m.device, who, m.args.get("minutes")))
        if m.op == "unlock":
            return ok(self.unlock(m.device, who))
        dev = self.device(m.device)
        if m.op == "describe":
            return ok(dev.describe())
        if m.op == "get":
            a = dev.attr(m.name)
            return ok(dev.get(m.name, m.channel), unit=a.unit or None)
        self.check_lock(m.device, who)
        if m.op == "set":
            a = dev.attr(m.name)
            return ok(dev.set(m.name, m.value, m.channel), unit=a.unit or None)
        if m.op == "call":
            return ok(dev.call(m.name, m.args, m.channel))
        raise LabratError("bad_request", f"unhandled op {m.op}")

    def open_stream(self, device: str, name: str, channel: str | None, who: Principal):
        """(content type, iterator of bytes) for a device's stream. Anyone who may read may watch."""
        if not role_allows(who.role, "get"):
            raise LabratError("forbidden", f"{who.name} may not watch streams")
        return self.device(device).open_stream(name, channel)

    def device(self, name: str) -> Device:
        if name not in self.devices:
            raise LabratError("not_found", f"no device {name!r}; there is {', '.join(self.devices) or 'none'}")
        return self.devices[name]

    def health(self, challenge: str | None = None) -> dict:
        return {"name": self.name, "role": self.role, "version": __version__, "uptime_s": round(time.time() - self.started)}

    def list_devices(self) -> list[dict]:
        out = []
        for n, d in self.devices.items():
            holder = self.holder(n)
            out.append({"device": n, "kind": d.kind, "driver": d.driver, "description": d.description,
                        **({"locked_by": holder} if holder else {})})
        return out

    # -- locks ----------------------------------------------------------------------------------------------
    def holder(self, device: str) -> str | None:
        with self._state:
            held = self.locks.get(device)
            if held and held[1] < time.monotonic():
                del self.locks[device]
                held = None
            return held[0] if held else None

    def exists(self, device: str) -> None:
        self.device(device)

    def lock(self, device: str, who: Principal, minutes: Any = None) -> dict:
        self.exists(device)
        minutes = convert("float", minutes, "minutes") if minutes is not None else self.lock_minutes
        if not 0 < minutes <= 24 * 60:
            raise LabratError("bad_request", "minutes is between 0 and 1440")
        holder = self.holder(device)
        if holder and holder != who.name:
            raise LabratError("locked", f"{device} is locked by {holder}")
        with self._state:
            self.locks[device] = (who.name, time.monotonic() + minutes * 60, minutes)
        return {"device": device, "locked_by": who.name, "minutes": minutes}

    def unlock(self, device: str, who: Principal) -> dict:
        holder = self.holder(device)
        if holder and holder != who.name and who.role != "admin":
            raise LabratError("locked", f"{device} is locked by {holder}")
        with self._state:
            self.locks.pop(device, None)
        return {"device": device, "locked_by": None}

    def check_lock(self, device: str, who: Principal) -> None:
        holder = self.holder(device)
        if holder and holder != who.name:
            raise LabratError("locked", f"{device} is locked by {holder}")
        if holder:                                            # use keeps a lock alive, for the time its holder chose
            with self._state:
                held = self.locks.get(device)
                if held:
                    self.locks[device] = (held[0], time.monotonic() + held[2] * 60, held[2])

    # -- safety ---------------------------------------------------------------------------------------------
    def stop(self, who: Principal | None = None, reason: str = "") -> dict:
        """Put every device in its safe state. Locks stay with their holders. Never raises."""
        problems: list[str] = []
        for d in self.devices.values():
            try:
                problems += d.make_safe()
            except Exception as e:
                problems.append(f"{d.name}: {e}")
        log.warning("stop by %s%s%s", who.name if who else "the server", ": " if reason else "", reason)
        for p in problems:
            log.error("stop: %s", p)
        return {"safe": not problems, "problems": problems}

    def shutdown(self) -> dict:
        """The safe state for this server's own devices only. A nest that restarts leaves its rats as they are:
        their watchdogs act if it stays away."""
        return Hub.stop(self, None, "shutdown")

    def close(self) -> None:
        for d in self.devices.values():
            try:
                d.close()
            except Exception:
                log.exception("closing %s", d.name)

    # -- audit ----------------------------------------------------------------------------------------------
    def audit(self, who: Principal, msg: Any, success: bool, err: LabratError | None = None) -> None:
        if self.audit_path is None:
            return
        m = msg.to_dict() if isinstance(msg, Message) else {k: msg.get(k) for k in ("op", "device", "name", "channel", "value", "args")}
        m = {k: v for k, v in m.items() if v is not None}
        if m.get("op") == "adopt":
            m.pop("value", None)                              # a rat's key never goes into the log
        if isinstance(m.get("args"), dict):
            m["args"] = {k: ("<hidden>" if any(s in k.lower() for s in SECRET_ARGS) else v) for k, v in m["args"].items()}
        entry = {"time": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "who": who.name, "role": who.role, **m, "ok": success}
        if err is not None:
            entry["error"] = err.code
        line = json.dumps(entry, default=str)
        try:
            with self._audit:
                self.audit_path.parent.mkdir(parents=True, exist_ok=True)
                with open(self.audit_path, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
        except OSError as e:                                  # the change happened: say so, and say the log failed
            log.error("cannot write the audit log %s: %s", self.audit_path, e)
