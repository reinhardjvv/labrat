"""The computers that run jobs for a nest: who joined them, whether they are shared, and what they report.

    labrat worker join              # on the computer, with its owner's token: a personal worker
    labrat worker join --shared     # offered to others as well, once an admin agrees
    labrat worker share gpu-box     # an admin agrees
    labrat workers                  # every worker, online or not, and what it has

A personal worker runs its owner's jobs only. A shared one also runs the jobs of the people its owner lets it
take (people in worker.toml), and only after an admin has agreed: a laptop cannot start taking other people's
work, or their data, without both. The nest keeps workers in <state_dir>/workers.json (readable by its owner
only, as it holds their keys). Workers talk to the nest through POST /api/v1/worker with signed requests
(signing.py), never with the key itself.

Everyone sees each worker's name, owner and what it can do. Its host name, platform, addresses and link address
are shown to its owner and to admins only.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from fnmatch import fnmatch
from pathlib import Path

from . import signing
from .config import check_name
from .message import LabratError, Message, role_allows

log = logging.getLogger("labrat")
ONLINE_S = 60                    # a worker that has not asked for work for this long is offline
WAIT_MAX = 25                    # the longest a worker's request for work is held open
PRIVATE = ("host", "platform", "python", "labrat", "addresses", "link")


def _now(t: float | None = None) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(t))


def _num(v, kind=float, lo=0, hi=1e9):
    try:
        x = kind(v)
    except (TypeError, ValueError):
        return None
    return x if lo <= x <= hi else None


def _text(v, n=80) -> str | None:
    return str(v)[:n] if isinstance(v, (str, int, float)) else None


def clean_report(r: dict) -> dict:
    """What a worker reports, with only known fields of the right types kept."""
    if not isinstance(r, dict):
        return {}
    out: dict = {}
    for k, kind, hi in (("cpus", int, 100000), ("slots", int, 100000), ("memory_gb", float, 1e7), ("load", float, 1e6)):
        if (v := _num(r.get(k), kind, 0, hi)) is not None:
            out[k] = v
    for k in ("gpu_busy", "idle", "accepting"):
        if isinstance(r.get(k), bool):
            out[k] = r[k]
    for k in ("backend", "accept", "when", "hours", *PRIVATE):
        if k != "addresses" and (v := _text(r.get(k), 200)) is not None:
            out[k] = v
    if isinstance(r.get("addresses"), list):
        out["addresses"] = [a for a in (_text(x, 64) for x in r["addresses"][:16]) if a]
    if isinstance(r.get("people"), list):
        out["people"] = [p for p in (_text(x, 63) for x in r["people"][:64]) if p]
    if isinstance(r.get("gpus"), list):
        out["gpus"] = [{"name": _text(g.get("name")) or "GPU", "memory_gb": _num(g.get("memory_gb"), float, 0, 1e5) or 0.0,
                        "busy": bool(g.get("busy"))} for g in r["gpus"][:64] if isinstance(g, dict)]
    if isinstance(r.get("stores"), list):
        out["stores"] = [{"name": s["name"], "available": bool(s.get("available")),
                          **({"free_gb": f} if (f := _num(s.get("free_gb"), float, 0, 1e9)) is not None else {})}
                         for s in r["stores"][:64] if isinstance(s, dict) and isinstance(s.get("name"), str)]
    if isinstance(r.get("limits"), dict):
        out["limits"] = {k: v for k in ("cpus", "gpus", "memory_gb")
                         if (v := _num(r["limits"].get(k), float, 0, 1e7)) is not None}
    return out


class Workers:
    def __init__(self, hub, path: Path):
        self.hub, self.path = hub, path
        self.entries: dict[str, dict] = {}                    # name -> owner, key, fingerprint, shared, joined
        self.live: dict[str, dict] = {}                       # name -> the latest report and when it came
        self.nonces = signing.Nonces()
        self._lock = threading.Lock()
        self.changed = threading.Condition(self._lock)        # a job was queued, stopped or a worker came
        if path.is_file():
            self.entries = json.loads(path.read_text())

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(self.entries, f, indent=2)
        os.replace(tmp, self.path)

    # -- state ----------------------------------------------------------------------------------------------
    def online(self, name: str) -> bool:
        seen = self.live.get(name, {}).get("_t")
        return seen is not None and time.monotonic() - seen < ONLINE_S

    def report(self, name: str) -> dict:
        return self.live.get(name, {}).get("report", {})

    def allows(self, name: str, person: str) -> bool:
        """True when this worker may run this person's jobs: its owner's always, others' once shared and allowed."""
        e = self.entries.get(name)
        if e is None:
            return False
        if person == e["owner"]:
            return True
        return e["shared"] == "yes" and any(fnmatch(person, p) for p in self.report(name).get("people", ["*"]))

    def public(self, name: str, who) -> dict:
        e, live = self.entries[name], self.live.get(name, {})
        r = dict(live.get("report", {}))
        private = who.role == "admin" or who.person == e["owner"]
        if not private:
            for k in PRIVATE:
                r.pop(k, None)
        out = {"name": name, "owner": e["owner"], "shared": e["shared"], "fingerprint": e["fingerprint"],
               "online": self.online(name), "joined": e["joined"], **r}
        if live.get("seen"):
            out["last_seen"] = live["seen"]
        return out

    # -- the operation --------------------------------------------------------------------------------------
    def op(self, m: Message, who) -> dict | list:
        action = m.args.get("action", "list")
        if action == "list":
            return [self.public(n, who) for n in sorted(self.entries)]
        if not m.name:
            raise LabratError("bad_request", f"{action} needs the worker's name")
        if action == "join":
            return self.join(m.name, m.args.get("key"), bool(m.args.get("shared")), who)
        e = self.entries.get(m.name)
        if e is None:
            raise LabratError("not_found", f"no worker {m.name!r}")
        if action == "show":
            return self.public(m.name, who)
        if action == "share":
            if who.role != "admin":
                raise LabratError("forbidden", f"{who.name} ({who.role}) may not share a worker; an admin does that")
            if e["shared"] == "no":
                raise LabratError("bad_request", f"{m.name}'s owner has not offered it to others (labrat worker join --shared)")
            return self._set_shared(m.name, "yes")
        if who.role != "admin" and who.name != e["owner"]:
            raise LabratError("forbidden", f"{m.name} belongs to {e['owner']}; only they or an admin may change it")
        if action == "unshare":
            return self._set_shared(m.name, "no")
        if action == "remove":
            return self.remove(m.name)
        raise LabratError("bad_request", "args.action is list, show, join, share, unshare or remove")

    def join(self, name: str, key, shared: bool, who) -> dict:
        check_name(name, "worker")
        if not role_allows(who.role, "set"):
            raise LabratError("forbidden", f"{who.name} ({who.role}) may not join a worker")
        if who.for_:
            raise LabratError("forbidden", f"{who.name} acts for {who.for_}; a person joins their own computers")
        if not isinstance(key, str) or not 32 <= len(key) <= 256:
            raise LabratError("bad_request", "join needs the worker's key (labrat worker join sends it)")
        from .rat import fingerprint
        with self._lock:
            old = self.entries.get(name)
            if old is not None and old["owner"] != who.name and who.role != "admin":
                raise LabratError("bad_request", f"there is already a worker {name!r}, joined by {old['owner']}")
            self.entries[name] = {"owner": old["owner"] if old else who.name, "key": key, "fingerprint": fingerprint(key),
                                  "shared": "asked" if shared else "no", "joined": _now()}
            self.live.pop(name, None)
            self._save()
        log.info("worker %s joined by %s%s", name, who.name, " (offered to others)" if shared else "")
        e = self.entries[name]
        return {"name": name, "owner": e["owner"], "shared": e["shared"], "fingerprint": e["fingerprint"],
                "nest": self.hub.name}

    def _set_shared(self, name: str, state: str) -> dict:
        with self._lock:
            self.entries[name]["shared"] = state
            self._save()
            self.changed.notify_all()
        return {"name": name, "shared": state}

    def remove(self, name: str) -> dict:
        with self._lock:
            self.entries.pop(name, None)
            self.live.pop(name, None)
            self._save()
            self.changed.notify_all()
        jobs = getattr(self.hub, "jobs", None)
        if jobs is not None:
            jobs.worker_gone(name)
        return {"name": name, "removed": True}

    # -- the worker protocol --------------------------------------------------------------------------------
    def request(self, header: str | None, body: bytes) -> tuple[int, bytes, str | None]:
        """Handle one signed request: (HTTP status, the reply's bytes, their signature or None when unsigned)."""
        try:
            name, ts, nonce, sig = signing.parse_header(header)
            e = self.entries.get(name)
            if e is None:
                raise LabratError("unauthorised", f"no worker {name!r} has joined this nest")
            signing.check_request(e["key"], ts, nonce, sig, body)
            if not self.nonces.fresh(nonce):
                raise LabratError("unauthorised", "this signed request was already used")
        except LabratError as err:
            return err.status, json.dumps({"ok": False, "error": {"code": err.code, "message": err.message}}).encode(), None
        try:
            obj = json.loads(body or b"{}")
            if not isinstance(obj, dict):
                raise LabratError("bad_request", "a worker request is a JSON object")
            reply = {"ok": True, "value": self.handle(name, obj)}
            status = 200
        except LabratError as err:
            reply, status = {"ok": False, "error": {"code": err.code, "message": err.message}}, err.status
        except Exception:
            log.exception("worker request from %s", name)
            reply, status = {"ok": False, "error": {"code": "internal", "message": "unexpected error; see the nest's log"}}, 500
        data = json.dumps(reply, default=str).encode()
        return status, data, signing.reply_signature(e["key"], nonce, data)

    def handle(self, name: str, obj: dict):
        action = obj.get("action")
        if "report" in obj:
            with self._lock:
                came_back = not self.online(name)
                self.live[name] = {"report": clean_report(obj["report"]), "_t": time.monotonic(), "seen": _now()}
                if came_back:
                    self.changed.notify_all()
            if came_back:
                log.info("worker %s is online", name)
        e = self.entries[name]
        if action == "leave":                                  # labrat setup --undo on the computer itself
            self.remove(name)
            log.info("worker %s left", name)
            return {"name": name, "removed": True}
        if action == "hello":
            return {"name": name, "owner": e["owner"], "shared": e["shared"], "nest": self.hub.name}
        jobs = getattr(self.hub, "jobs", None)
        if action == "next":
            wait = min(max(float(obj.get("wait", 0) or 0), 0.0), WAIT_MAX)
            if jobs is not None:
                return jobs.next_for(name, wait, obj.get("running") or [])
            with self._lock:
                self.changed.wait(wait)
            return {"parts": [], "stop": []}
        if jobs is not None:
            return jobs.from_worker(name, action, obj)
        raise LabratError("bad_request", f"unknown worker action {action!r}")

    def who(self) -> list[dict]:
        jobs = getattr(self.hub, "jobs", None)
        out = []
        for name in sorted(self.entries):
            row = {"worker": name, "owner": self.entries[name]["owner"], "online": self.online(name)}
            if jobs is not None:
                row["running"] = jobs.running_on(name)
            out.append(row)
        return out
