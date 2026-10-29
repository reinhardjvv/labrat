"""The core shared by the nest and the rats: devices, who may do what, locks, the stop and the audit log."""
from __future__ import annotations

import hashlib
import itertools
import json
import logging
import threading
import time
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path
from typing import Any

from . import __version__
from .device import Device, convert
from .message import LabratError, Message, fail, ok, parse, role_allows

log = logging.getLogger("labrat")
SECRET_ARGS = ("key", "password", "token", "secret")


@dataclass(frozen=True)
class Principal:
    """Who sends a request. A token may act for a person (an agent's token, for one): its work then counts as
    that person's, in their experiments and folders, and the logs say which token did it."""
    name: str
    role: str
    for_: str | None = None
    god: bool = False                     # god mode: no lab limits or approvals; other people's locks still hold

    @property
    def person(self) -> str:
        return self.for_ or self.name


@dataclass
class Rule:
    """Requests that wait for a person: who sends them, which ops, devices and names (shell-style patterns)."""
    who: list
    ops: list
    devices: list
    names: list

    def matches(self, m: Message, who: Principal) -> bool:
        return (any(fnmatch(who.name, w) for w in self.who) and m.op in self.ops
                and any(fnmatch(m.device or "", d) for d in self.devices) and any(fnmatch(m.name or "", n) for n in self.names))

    @classmethod
    def from_dict(cls, d: dict) -> Rule:
        unknown = set(d) - {"who", "ops", "devices", "names"}
        if unknown:
            raise LabratError("bad_request", f"[[approvals]]: unknown setting(s) {', '.join(sorted(unknown))}")
        ops = list(d.get("ops", ["set", "call"]))
        if not set(ops) <= {"set", "call", "lock"}:
            raise LabratError("bad_request", "[[approvals]] ops are set, call and lock")
        return cls(list(d.get("who", ["*"])), ops, list(d.get("devices", ["*"])), list(d.get("names", ["*"])))


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class Hub:
    role = "hub"

    def __init__(self, name: str, devices: dict[str, Device] | None = None, tokens: dict[str, Principal] | None = None,
                 audit_path: Path | None = None, lock_minutes: float = 10.0, approvals: list | None = None,
                 approval_minutes: float = 30.0, require_lock=None):
        self.name = name
        self.devices: dict[str, Device] = dict(devices or {})
        self.tokens: dict[str, Principal] = dict(tokens or {})       # sha256 of the token -> who it is
        self.audit_path = audit_path
        self.lock_minutes = lock_minutes
        self.locks: dict[str, tuple[str, float, float]] = {}          # device -> (holder, expiry on time.monotonic, minutes)
        self._state = threading.Lock()
        self._audit = threading.Lock()
        self.started = time.time()
        self.rules = [Rule.from_dict(r) for r in (approvals or [])]
        self.approval_minutes = approval_minutes
        self.pending: dict[str, dict] = {}                            # id -> request, requester, state, result
        self.recorder = None                                          # set by the nest or rat when it has stores
        self.experiments = None                                       # set by the nest when it has [experiments]
        self._ids = itertools.count(1)
        self.active_at: dict[str, float] = {}                         # person -> time of their last request
        self.last_change: dict[str, dict] = {}                        # device -> who changed it last, when and how
        self.users = None                                             # set by the nest: people added while it runs
        self._ctx = threading.local()
        if require_lock is True:
            self.require_lock = ["*"]
        elif isinstance(require_lock, list):
            self.require_lock = [str(p) for p in require_lock]
        elif require_lock in (None, False):
            self.require_lock = []
        else:
            raise LabratError("bad_request", "require_lock is true, false or a list of device patterns")
        self._recent()

    # -- who is asking --------------------------------------------------------------------------------------
    def authenticate(self, header: str | None) -> Principal:
        if not header or not header.startswith("Bearer "):
            raise LabratError("unauthorised", "send Authorization: Bearer <token>")
        who = self.tokens.get(token_hash(header[7:].strip()))
        if who is None:
            raise LabratError("unauthorised", "unknown token")
        self.active_at[who.name] = time.time()
        return who

    # -- handling messages ----------------------------------------------------------------------------------
    def handle(self, obj: Any, who: Principal) -> dict:
        msg_id = obj.get("id") if isinstance(obj, dict) else None
        try:
            msg = parse(obj)
            if not role_allows(who.role, msg.op):
                raise LabratError("forbidden", f"{who.name} ({who.role}) may not {msg.op}")
            if not who.god and any(r.matches(msg, who) for r in self.rules):
                return fail(self.ask_approval(msg, who), msg_id)
            reply = self.dispatch(msg, who)
            if msg.op in ("set", "call", "lock", "unlock", "stop", "adopt", "forget", "deny") or (
                    msg.op == "users" and msg.args.get("action") in ("add", "remove")) or (
                    msg.op in ("record", "experiment") and msg.args.get("action") in ("start", "stop", "note")):
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
        if m.op in ("approvals", "approval", "approve", "deny"):
            return ok(self.approvals_op(m, who))
        if m.op == "record":
            if self.recorder is None:
                raise LabratError("not_found", "this server has no stores for recordings")
            if m.args.get("action") in ("start", "stop") and not role_allows(who.role, "set"):
                raise LabratError("forbidden", f"{who.name} ({who.role}) may not start or stop recordings")
            return ok(self.recorder.op(m, who))
        if m.op in ("experiment", "files"):
            if self.experiments is None:
                raise LabratError("not_found", "this server keeps no experiments (add an [experiments] table to its configuration)")
            if m.op == "experiment" and m.args.get("action") in ("start", "stop", "note") and not role_allows(who.role, "set"):
                raise LabratError("forbidden", f"{who.name} ({who.role}) may not start, stop or write in experiments")
            return ok(self.experiments.op(m, who) if m.op == "experiment" else self.experiments.files(m, who))
        if m.op == "health":
            return ok(self.health())
        if m.op == "who":
            return ok(self.who())
        if m.op == "users":
            if self.users is None:
                raise LabratError("not_found", f"users are added at the nest; this is a {self.role}")
            return ok(self.users.op(m, who))
        if m.op == "list":
            return ok(self.list_devices(m.args))
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
            return ok(dev.set(m.name, m.value, m.channel, self.god(m, who)), unit=a.unit or None)
        if m.op == "call":
            return ok(dev.call(m.name, m.args, m.channel))
        raise LabratError("bad_request", f"unhandled op {m.op}")

    def god(self, m: Message, who: Principal) -> bool:
        """True when this request skips the lab's limits."""
        return who.god

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

    def list_devices(self, args: dict | None = None) -> list[dict]:
        """Devices with their identity, filtered by args.select (a selector) and ordered by args.sort."""
        from .ident import matches, parse_selector, sort
        args = args or {}
        out = self.entries()
        refs: dict[str, str] = {}
        for e in out:
            if e.get("ref"):
                if e["ref"] in refs:
                    e["duplicate_of"] = refs[e["ref"]]       # the same instrument configured twice
                else:
                    refs[e["ref"]] = e["device"]
        if args.get("select"):
            sel = parse_selector(str(args["select"]))
            out = [e for e in out if matches(e, sel)]
        return sort(out, str(args.get("sort", "device")))

    def entries(self) -> list[dict]:
        out = []
        for n, d in self.devices.items():
            holder = self.holder(n)
            out.append({"device": n, **d.identity(), "driver": d.driver, "description": d.description,
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

    def lock_required(self, device: str) -> bool:
        return any(fnmatch(device, p) for p in self.require_lock) and not getattr(self._ctx, "recording", False)

    def check_lock(self, device: str, who: Principal) -> None:
        holder = self.holder(device)
        if holder and holder != who.name:
            raise LabratError("locked", f"{device} is locked by {holder}")
        if holder is None and self.lock_required(device):
            raise LabratError("locked", f"lock {device} before changing it (labrat lock {device})")
        if holder:                                            # use keeps a lock alive, for the time its holder chose
            with self._state:
                held = self.locks.get(device)
                if held:
                    self.locks[device] = (held[0], time.monotonic() + held[2] * 60, held[2])

    def recording(self):
        """Requests made by a recording in this thread: a lock is not required for them, as nobody is there to
        take one. Locks other people hold still apply."""
        ctx = self._ctx

        class Recording:
            def __enter__(self):
                ctx.recording = True

            def __exit__(self, *exc):
                ctx.recording = False
        return Recording()

    def forget_person(self, name: str) -> None:
        """A removed user's locks are released and their waiting requests denied."""
        with self._state:
            for dev in [d for d, held in self.locks.items() if held[0] == name]:
                del self.locks[dev]
            for p in self.pending.values():
                if p["by"] == name and p["state"] == "pending":
                    p.update(state="denied", decided_by="user removed")

    # -- who is using what ----------------------------------------------------------------------------------
    def _recent(self) -> None:
        """The last change to each device, from the end of the audit log, so 'who' survives a restart."""
        if self.audit_path is None or not self.audit_path.is_file():
            return
        try:
            with open(self.audit_path, "rb") as f:
                start = max(0, f.seek(0, 2) - (2 << 20))
                f.seek(start)
                tail = f.read().decode(errors="replace").splitlines()[1 if start else 0:]   # the first line may be cut
        except OSError:
            return
        for line in tail:
            try:
                e = json.loads(line)
            except ValueError:
                continue
            self._note(e)

    def _note(self, entry: dict) -> None:
        if entry.get("op") in ("set", "call") and entry.get("ok") and entry.get("device"):
            self.last_change[entry["device"]] = {k: entry[k] for k in ("time", "who", "op", "name", "channel", "value", "for")
                                                 if entry.get(k) is not None}

    def who(self) -> dict:
        """Who holds each device, who changed it last, and when everyone was last active."""
        now = time.monotonic()
        with self._state:
            locks = {d: (h, max(0.0, (exp - now) / 60)) for d, (h, exp, _) in self.locks.items() if exp > now}
        devices = []
        for dev in sorted(set(locks) | set(self.last_change)):              # devices someone holds or has changed
            row = {"device": dev}
            if dev in locks:
                row.update(locked_by=locks[dev][0], minutes_left=round(locks[dev][1], 1))
            if dev in self.last_change:
                row["last_change"] = self.last_change[dev]
            devices.append(row)
        people = [{"name": n, "last_active": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(t))}
                  for n, t in sorted(self.active_at.items(), key=lambda x: -x[1]) if n != "nest"]
        return {"devices": devices, "people": people,
                **({"require_lock": self.require_lock} if self.require_lock else {})}

    # -- approvals ------------------------------------------------------------------------------------------
    def ask_approval(self, m: Message, who: Principal) -> LabratError:
        self._expire()
        aid = f"a{next(self._ids)}"
        with self._state:
            self.pending[aid] = {"id": aid, "by": who.name, "request": m.to_dict(), "state": "pending",
                                 "asked": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "_t": time.monotonic(), "_msg": m, "_who": who}
        self.audit(who, {"op": "request", "device": m.device, "name": m.name, "channel": m.channel, "value": m.value, "args": m.args}, True)
        log.info("approval %s: %s asks to %s %s.%s", aid, who.name, m.op, m.device, m.name)
        return LabratError("pending", f"{m.op} {m.device}.{m.name} waits for a person to approve it (approval {aid})", approval=aid)

    def _expire(self) -> None:
        limit = time.monotonic() - self.approval_minutes * 60
        with self._state:
            for p in self.pending.values():
                if p["state"] == "pending" and p["_t"] < limit:
                    p["state"] = "expired"

    @staticmethod
    def _public(p: dict) -> dict:
        return {k: v for k, v in p.items() if not k.startswith("_")}

    def approvals_op(self, m: Message, who: Principal):
        self._expire()
        if m.op == "approvals":
            return [self._public(p) for p in self.pending.values() if p["state"] == "pending"]
        p = self.pending.get(m.name or "")
        if p is None:
            raise LabratError("not_found", f"no approval {m.name!r}")
        if m.op == "approval":
            return self._public(p)
        if p["state"] != "pending":
            raise LabratError("bad_request", f"approval {p['id']} is {p['state']}")
        if p["by"] == who.name:
            raise LabratError("forbidden", "a request needs someone other than the person who asked to approve it")
        if m.op == "approve" and who.for_:
            raise LabratError("forbidden", f"{who.name} acts for {who.for_} and may not approve requests; a person does that")
        if m.op == "deny":
            p.update(state="denied", decided_by=who.name)
            return self._public(p)
        p.update(state="approved", decided_by=who.name)
        try:
            reply = self.dispatch(p["_msg"], p["_who"])           # the requester's own rights, limits and locks apply
            p["result"] = {k: v for k, v in reply.items() if k != "ok"}
        except LabratError as e:
            p.update(state="failed", error={"code": e.code, "message": e.message})
        self.audit(who, {"op": "approve", "name": p["id"], "device": p["request"].get("device")}, p["state"] == "approved")
        return self._public(p)

    # -- safety ---------------------------------------------------------------------------------------------
    def stop(self, who: Principal | None = None, reason: str = "") -> dict:
        """Put every device in its safe state. Locks stay with their holders. Never raises."""
        problems: list[str] = []
        with self._state:                                     # nothing waiting may go ahead after a stop
            for p in self.pending.values():
                if p["state"] == "pending":
                    p.update(state="denied", decided_by="stop")
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
        if self.recorder is not None:
            self.recorder.stop_all()
        if self.experiments is not None:
            self.experiments.close()
        for s in (self.recorder.stores.values() if self.recorder is not None else ()):
            s.close()
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
        if m.get("op") == "users":
            m.pop("value", None)
        if isinstance(m.get("args"), dict):
            m["args"] = {k: ("<hidden>" if any(s in k.lower() for s in SECRET_ARGS) else v) for k, v in m["args"].items()}
        entry = {"time": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "who": who.name, "role": who.role,
                 **({"for": who.for_} if who.for_ else {}), **({"god": True} if who.god else {}), **m, "ok": success}
        if err is not None:
            entry["error"] = err.code
        if self.experiments is not None:
            self.experiments.log(who, entry)
        self._note(entry)
        line = json.dumps(entry, default=str)
        try:
            with self._audit:
                self.audit_path.parent.mkdir(parents=True, exist_ok=True)
                with open(self.audit_path, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
        except OSError as e:                                  # the change happened: say so, and say the log failed
            log.error("cannot write the audit log %s: %s", self.audit_path, e)
