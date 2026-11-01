"""A rat: a small computer next to the equipment that serves its devices to a nest.

A rat runs the same message API as the nest, for the devices plugged into it (GPIO lines, serial and USB
instruments, instruments on its own network). It announces itself on the network, and the nest adopts it with
the rat's key. The key is made on first start and kept in the state directory; 'labrat rat key' shows it.

With watchdog_s set (30 s by default), a rat that has changed an output and then hears nothing from its nest for
that long puts its devices in their safe state. A crashed nest or a cut network cable then leaves the
equipment safe.
"""
from __future__ import annotations

import copy
import hashlib
import hmac
import logging
import os
import secrets
import threading
import time
from pathlib import Path

from . import config as conf
from . import lease as leases
from .drivers import build
from .hub import Hub, Principal, token_hash
from .message import LabratError, Message, ok

log = logging.getLogger("labrat")


def key_path(state_dir: Path) -> Path:
    return Path(state_dir) / "rat.key"


def load_or_create_key(state_dir: Path) -> str:
    p = key_path(state_dir)
    if p.is_file():
        return p.read_text().strip()
    p.parent.mkdir(parents=True, exist_ok=True)
    key = secrets.token_urlsafe(24)
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(key + "\n")
    log.info("made a new rat key in %s", p)
    return key


def fingerprint(key: str) -> str:
    h = hashlib.sha256(key.encode()).hexdigest()[:12]
    return "-".join(h[i:i + 4] for i in range(0, 12, 4))


def proof(key: str, challenge: str) -> str:
    return hmac.new(key.encode(), challenge.encode(), hashlib.sha256).hexdigest()


class Rat(Hub):
    role = "rat"

    def __init__(self, cfg: dict, devices: dict | None = None):
        from .catalog import configure
        configure(cfg.get("catalog") or [Path(cfg["state_dir"]) / "profiles"])
        self.key = load_or_create_key(cfg["state_dir"])
        self.fingerprint = fingerprint(self.key)
        tokens = conf.tokens(cfg)
        self.nest = Principal("nest", "operator")
        tokens[token_hash(self.key)] = self.nest
        self.allow_god_mode = bool(cfg.get("allow_god_mode", False))
        if devices is None:
            devices = {n: build(n, d) for n, d in cfg["devices"].items()}
        super().__init__(cfg["name"], devices, tokens, Path(cfg["state_dir"]) / "audit.jsonl", float(cfg["lock_minutes"]),
                         cfg.get("approvals"), float(cfg.get("approval_minutes", 30)))
        if cfg.get("stores") or cfg.get("recordings"):
            from .record import Recorder
            from .store import stores_from
            self.recorder = Recorder(self, stores_from(cfg), cfg.get("recordings") or {})
        from .bench import Workbench
        self.bench = Workbench(self, Path(cfg["state_dir"]))
        self.watchdog_s = float(cfg.get("watchdog_s") or 0)
        self.last_seen = time.monotonic()
        self.armed = True                         # after a restart, outputs may still be on from before
        self._stop = threading.Event()
        self.leases: dict[str, dict] = {}         # id -> who, devices, limits, exp (this rat's clock), armed, commands
        self.ended: dict[str, float] = {}         # leases released, revoked or run out -> their exp, refused until then
        if self.watchdog_s > 0:
            threading.Thread(target=self._watch, name="labrat-watchdog", daemon=True).start()
        threading.Thread(target=self._lease_watch, name="labrat-leases", daemon=True).start()

    def authenticate(self, header):
        if header and header.startswith("Lease "):
            who = self._lease(header[6:].strip())
        else:
            who = super().authenticate(header)
        self.last_seen = time.monotonic()
        return who

    # -- leases: direct access granted by the nest ----------------------------------------------------------
    def _lease(self, token: str) -> Principal:
        body = leases.verify(self.key, token)
        lid = str(body["id"])
        if lid in self.ended:
            raise LabratError("unauthorised", f"lease {lid} has ended")
        devices = tuple(str(d) for d in body["devices"])
        for d in devices:
            self.device(d)
        with self._state:
            held = self.leases.setdefault(lid, {"who": body["who"], "devices": devices, "armed": False, "commands": 0})
            held.update(exp=float(body["exp"]), limits=body.get("limits") or {})
        self.active_at[str(body["who"])] = time.time()
        return Principal(str(body["who"]), "operator", body.get("for") or None, job=body.get("job"), devices=devices, lease=lid)

    def leased_to(self, device: str) -> tuple[str, dict] | None:
        now = time.time()
        for lid, held in list(self.leases.items()):
            if device in held["devices"] and held["exp"] >= now:
                return lid, held
        return None

    def _lease_scope(self, m: Message, who: Principal) -> None:
        if m.op not in ("health", "list", "describe", "get", "set", "call", "stop"):
            raise LabratError("forbidden", f"a lease is for {', '.join(who.devices)}; {m.op} goes through the nest")
        if m.device is not None and m.device not in who.devices:
            raise LabratError("forbidden", f"lease {who.lease} is for {', '.join(who.devices)}, not {m.device}")

    def end_lease(self, lid: str, why: str, safe: bool) -> dict:
        """Close a lease: its devices to their safe state if it changed something and safe is set, its log flushed."""
        with self._state:
            held = self.leases.pop(lid, None)
            if held is not None:
                self.ended[lid] = held["exp"]
        if held is None:
            return {"id": lid, "commands": 0}
        problems = []
        if safe and held["armed"]:
            log.warning("lease %s of %s %s: putting %s in the safe state", lid, held["who"], why, ", ".join(held["devices"]))
            for d in held["devices"]:
                try:
                    problems += self.devices[d].make_safe()
                except Exception as e:
                    problems.append(f"{d}: {e}")
        self.flush_streams(lid)
        self.audit(Principal(held["who"], "operator", lease=lid), {"op": "lease", "name": lid, "args": {
            "action": why, "commands": held["commands"], **({"safe_state": True} if safe and held["armed"] else {})}}, not problems)
        return {"id": lid, "commands": held["commands"], **({"problems": problems} if problems else {})}

    def _lease_watch(self) -> None:
        while not self._stop.wait(0.5):
            now = time.time()
            for lid in [lid for lid, h in list(self.leases.items()) if h["exp"] < now]:
                self.end_lease(lid, "ran out", safe=True)
            for lid in [lid for lid, exp in list(self.ended.items()) if exp < now - 60]:
                self.ended.pop(lid, None)
            self.flush_streams(older_than=10.0)

    def handle(self, obj, who: Principal) -> dict:
        if who.lease is not None and isinstance(obj, dict):
            try:
                self._lease_scope(Message(op=str(obj.get("op")), device=obj.get("device")), who)
            except LabratError as e:
                from .message import fail
                return fail(e, obj.get("id"))
        return super().handle(obj, who)

    def dispatch(self, m: Message, who: Principal) -> dict:
        if m.op == "lease":
            if who is not self.nest:
                raise LabratError("forbidden", "leases are released and revoked by the nest")
            action = m.args.get("action")
            if action == "renew":
                with self._state:
                    held = self.leases.get(str(m.name))
                    if held is not None:
                        held["exp"] = max(held["exp"], float(m.args.get("exp", 0)))
                return ok({"id": m.name, "known": held is not None})
            if action not in ("release", "revoke"):
                raise LabratError("bad_request", "args.action is renew, release or revoke")
            return ok(self.end_lease(str(m.name), "released" if action == "release" else "revoked", safe=action == "revoke"))
        if m.op in ("set", "call") and m.device:
            held = self.leased_to(m.device)
            if held is not None and who is not self.nest and who.lease != held[0]:
                raise LabratError("locked", f"{m.device} is leased to {held[1]['who']}")
            if who.lease is not None and m.op == "set":
                a = copy.copy(self.device(m.device).attr(m.name))
                lim = (self.leases.get(who.lease, {}).get("limits") or {}).get(m.device, {}).get(m.name)
                if lim:
                    a.narrow(lim.get("min"), lim.get("max"))
                a.check(m.value)                          # the nest's limits, carried in the lease
        reply = super().dispatch(m, who)
        if m.op in ("set", "call"):
            self.armed = True
            if who.lease is not None and who.lease in self.leases:
                self.leases[who.lease]["armed"] = True
                self.leases[who.lease]["commands"] += 1
        elif m.op == "stop":
            self.armed = False
            for lid in list(self.leases):
                self.end_lease(lid, "ended by a stop", safe=False)       # the stop has just made everything safe
        elif m.op == "health" and who is self.nest:
            from .discovery import local_addresses
            reply["value"] = {**reply["value"], "addresses": local_addresses()}
        return reply

    def god(self, m: Message, who: Principal) -> bool:
        """A token of the rat's own in god mode, or the nest passing on god mode where this rat allows it."""
        if who.god:
            return True
        return who is self.nest and m.args.get("god") is True and self.allow_god_mode

    def health(self, challenge: str | None = None, lease: str | None = None) -> dict:
        h = {**super().health(), "fingerprint": self.fingerprint, "watchdog_s": self.watchdog_s, "time": time.time()}
        if challenge:                             # proves this rat holds the key without sending it
            h["proof"] = proof(self.key, challenge)
            if lease:                             # and, to a lease holder, that it is the rat the lease is for
                h["lease_proof"] = leases.proof(leases.secret(self.key, lease), challenge)
        return h

    def _watch(self) -> None:
        while not self._stop.wait(1.0):
            if self.armed and time.monotonic() - self.last_seen > self.watchdog_s:
                log.warning("no request for %.0f s: putting every device in its safe state", self.watchdog_s)
                self.armed = False                # first, so a change arriving during the stop arms it again
                self.stop(None, "watchdog")

    def close(self) -> None:
        self._stop.set()
        super().close()
