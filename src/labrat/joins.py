"""Computers asking to join a nest, as a worker, a rat or both, and the join keys that let them in at once.

    labrat setup                              # on the computer: asks to join and shows a code
    labrat approve HJKL-4821                  # anyone signed in: their own personal worker
    labrat approve HJKL-4821 --owner lab --shared      # an admin, for someone else or shared
    labrat key new --uses 20 --hours 24 --shared       # an admin: a join key for many computers
    labrat setup --key lk_...                 # on each: joins at once

A computer that asks sends the keys it made for itself (never anyone's token), and gets a short code that runs
out after ten minutes. Whoever approves the code sees the computer's name, fingerprint and what it has. An
operator may approve a computer as their own personal worker; joining as shared, for someone else, or as a rat
needs an admin. A join key carries those terms itself: how many computers it lets in, until when, whose they
are and whether they are shared. Only its SHA-256 is kept, in <state_dir>/joinkeys.json.

The asking computer has no token yet, so POST /api/v1/join is open. It only ever creates a waiting request:
at most 20 wait at once and 3 from one address, and nothing joins until a person or a join key says so.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import secrets
import threading
import time
from pathlib import Path

from .config import check_name
from .message import LabratError, Message, role_allows

log = logging.getLogger("labrat")
CODE_MINUTES = 10
MAX_WAITING, MAX_PER_ADDRESS = 20, 3
LETTERS = "HJKLMNPRTVWXY"                   # no letters that read as digits


def _now(t: float | None = None) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(t))


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def proof(key: str, request_id: str) -> str:
    """What the asking computer sends to show it is the one that asked."""
    return hmac.new(key.encode(), f"join:{request_id}".encode(), hashlib.sha256).hexdigest()


class Joins:
    def __init__(self, hub, state: Path):
        self.hub = hub
        self.path = state / "joinkeys.json"
        self.waiting: dict[str, dict] = {}                 # request id -> the request and its state
        self.keys: dict[str, dict] = json.loads(self.path.read_text()) if self.path.is_file() else {}
        self._lock = threading.Lock()

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(self.keys, f, indent=2)
        os.replace(tmp, self.path)

    def _expire(self) -> None:
        now = time.time()
        for r in self.waiting.values():
            if r["state"] == "waiting" and r["_expires"] < now:
                r["state"] = "expired"
        for rid in [rid for rid, r in self.waiting.items() if r["_expires"] < now - 3600]:
            del self.waiting[rid]

    @staticmethod
    def public(r: dict) -> dict:
        return {k: v for k, v in r.items() if not k.startswith("_")}

    # -- the asking computer ------------------------------------------------------------------------------
    def request(self, body: dict, address: str) -> dict:
        """POST /api/v1/join: ask (a new request) or status (how one stands)."""
        action = body.get("action")
        if action == "status":
            return self.status(str(body.get("id", "")), str(body.get("proof", "")))
        if action != "ask":
            raise LabratError("bad_request", "action is ask or status")
        name = str(body.get("name", ""))
        check_name(name, "computer")
        roles = [r for r in body.get("roles") or [] if r in ("worker", "rat")]
        if not roles:
            raise LabratError("bad_request", "roles is worker, rat or both")
        keys = {r: str(body.get(f"{r}_key") or "") for r in roles}
        if any(not 32 <= len(k) <= 256 for k in keys.values()):
            raise LabratError("bad_request", "each role needs the key this computer made for it")
        from .rat import fingerprint
        rat_port = int(body.get("rat_port") or 0)
        if "rat" in roles and not 0 < rat_port < 65536:
            raise LabratError("bad_request", "a rat gives the port it listens on")
        summary = {k: str(v)[:200] for k, v in (body.get("summary") or {}).items() if isinstance(k, str)}
        req = {"id": secrets.token_hex(8), "code": f"{''.join(secrets.choice(LETTERS) for _ in range(4))}-{secrets.randbelow(9000) + 1000}",
               "name": name, "roles": roles, "shared": bool(body.get("shared")), "summary": dict(list(summary.items())[:20]),
               "fingerprint": fingerprint(keys.get("worker") or keys["rat"]), "address": address, "asked": _now(),
               "state": "waiting", "_keys": keys, "_expires": time.time() + CODE_MINUTES * 60,
               **({"_rat_url": f"http://{address}:{rat_port}"} if "rat" in roles else {})}
        join_key = body.get("join_key")
        with self._lock:
            self._expire()
            if join_key:
                terms = self._use_key(str(join_key), roles)
                self.waiting[req["id"]] = req
            else:
                waiting = [r for r in self.waiting.values() if r["state"] == "waiting"]
                if len(waiting) >= MAX_WAITING or sum(r["address"] == address for r in waiting) >= MAX_PER_ADDRESS:
                    raise LabratError("unavailable", "too many computers are waiting to join; try again later")
                self.waiting[req["id"]] = req
        if join_key:
            self._admit(req, terms["owner"], terms["shared"], f"join key {terms['id']}")
        else:
            log.info("%s at %s asks to join as %s (code %s)", name, address, " and ".join(roles), req["code"])
        return {**self.public(req), "expires_s": CODE_MINUTES * 60, "nest": self.hub.name}

    def status(self, rid: str, given: str) -> dict:
        with self._lock:
            self._expire()
            r = self.waiting.get(rid)
        if r is None or not any(hmac.compare_digest(given, proof(k, rid)) for k in r["_keys"].values()):
            raise LabratError("not_found", "no such request to join")
        return {**self.public(r), "nest": self.hub.name}

    # -- people deciding ----------------------------------------------------------------------------------
    def op(self, m: Message, who) -> dict | list:
        action = m.args.get("action", "list")
        with self._lock:
            self._expire()
        if action == "list":
            return [self.public(r) for r in self.waiting.values() if r["state"] == "waiting"]
        if action in ("approve", "deny"):
            return self.decide(str(m.name or ""), who, action, m.args)
        if who.role != "admin":
            raise LabratError("forbidden", f"{who.name} ({who.role}) may not manage join keys; an admin does that")
        if action == "keys":
            return [{"id": k, **{x: v for x, v in e.items() if x != "sha256"}} for k, e in self.keys.items()]
        if action == "key_new":
            return self.new_key(m.args, who)
        if action == "key_revoke":
            with self._lock:
                if self.keys.pop(str(m.name or ""), None) is None:
                    raise LabratError("not_found", f"no join key {m.name!r}")
                self._save()
            return {"id": m.name, "revoked": True}
        raise LabratError("bad_request", "args.action is list, approve, deny, keys, key_new or key_revoke")

    def find(self, code: str) -> dict:
        code = code.strip().upper()
        for r in self.waiting.values():
            if r["code"] == code:
                return r
        raise LabratError("not_found", f"no computer is waiting with code {code}")

    def decide(self, code: str, who, action: str, args: dict) -> dict:
        if not role_allows(who.role, "set"):
            raise LabratError("forbidden", f"{who.name} ({who.role}) may not let computers join")
        if who.for_:
            raise LabratError("forbidden", f"{who.name} acts for {who.for_}; a person lets computers join")
        r = self.find(code)
        if r["state"] != "waiting":
            raise LabratError("bad_request", f"{r['name']}'s request is {r['state']}")
        if action == "deny":
            r.update(state="denied", decided_by=who.name)
            return self.public(r)
        owner = str(args.get("owner") or who.name)
        shared = bool(args["shared"]) if args.get("shared") is not None else r["shared"]
        if who.role != "admin" and (owner != who.name or shared or "rat" in r["roles"]):
            raise LabratError("forbidden", "an operator approves a computer as their own personal worker; "
                              "shared computers, someone else's and rats need an admin")
        check_name(owner, "person")
        return self._admit(r, owner, shared, who.name)

    def _admit(self, r: dict, owner: str, shared: bool, by: str) -> dict:
        """Let the computer in: its worker into the nest's workers, its rat adopted."""
        problems = []
        if "worker" in r["roles"]:
            from .rat import fingerprint
            ws = self.hub.workers
            with ws._lock:
                old = ws.entries.get(r["name"])
                if old is not None and old["owner"] != owner:
                    problems.append(f"a worker {r['name']!r} already belongs to {old['owner']}")
                else:
                    ws.entries[r["name"]] = {"owner": owner, "key": r["_keys"]["worker"], "fingerprint": fingerprint(r["_keys"]["worker"]),
                                             "shared": "yes" if shared else "no", "joined": _now()}
                    ws.live.pop(r["name"], None)
                    ws._save()
        if "rat" in r["roles"] and not problems:
            try:
                self.hub.adopt(r["name"], r["_keys"]["rat"], r["_rat_url"])
            except LabratError as e:
                problems.append(f"adopting the rat: {e.message}")
        if problems:
            r.update(state="failed", decided_by=by, error="; ".join(problems))
            raise LabratError("bad_request", "; ".join(problems))
        r.update(state="approved", decided_by=by, owner=owner, shared=shared, decided=_now())
        log.info("%s joined as %s, %s's%s (by %s)", r["name"], " and ".join(r["roles"]), owner, ", shared" if shared else "", by)
        return self.public(r)

    # -- join keys ------------------------------------------------------------------------------------------
    def new_key(self, args: dict, who) -> dict:
        uses, hours = int(args.get("uses", 1)), float(args.get("hours", 24))
        if not 1 <= uses <= 1000 or not 0 < hours <= 24 * 30:
            raise LabratError("bad_request", "uses is 1 to 1000 and hours up to 720")
        roles = [r for r in args.get("roles") or ["worker"] if r in ("worker", "rat")] or ["worker"]
        owner = str(args.get("owner") or who.name)
        check_name(owner, "person")
        key = "lk_" + secrets.token_urlsafe(24)
        kid = secrets.token_hex(4)
        with self._lock:
            self.keys[kid] = {"sha256": _hash(key), "uses_left": uses, "expires": time.time() + hours * 3600,
                              "expires_at": _now(time.time() + hours * 3600), "owner": owner, "shared": bool(args.get("shared")),
                              "roles": roles, "made_by": who.name, "made": _now()}
            self._save()
        return {"id": kid, "key": key, **{k: v for k, v in self.keys[kid].items() if k != "sha256"}}

    def _use_key(self, key: str, roles: list) -> dict:
        """Called with the lock held: the key's terms, one use spent, or LabratError."""
        h = _hash(key)
        for kid, e in self.keys.items():
            if hmac.compare_digest(e["sha256"], h):
                if e["expires"] < time.time() or e["uses_left"] < 1:
                    raise LabratError("unauthorised", "this join key has run out")
                if set(roles) - set(e["roles"]):
                    raise LabratError("forbidden", f"this join key lets computers in as {' and '.join(e['roles'])} only")
                e["uses_left"] -= 1
                self._save()
                return {"id": kid, **e}
        raise LabratError("unauthorised", "unknown join key")
