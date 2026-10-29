"""A rat: a small computer next to the equipment that serves its devices to a nest.

A rat runs the same message API as the nest, for the devices plugged into it (GPIO lines, serial and USB
instruments, instruments on its own network). It announces itself on the network, and the nest adopts it with
the rat's key. The key is made on first start and kept in the state directory; 'labrat rat key' shows it.

With watchdog_s set (30 s by default), a rat that has changed an output and then hears nothing from its nest for
that long puts its devices in their safe state. A crashed nest or a cut network cable then leaves the
equipment safe.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
import threading
import time
from pathlib import Path

from . import config as conf
from .drivers import build
from .hub import Hub, Principal, token_hash
from .message import Message

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
        self.watchdog_s = float(cfg.get("watchdog_s") or 0)
        self.last_seen = time.monotonic()
        self.armed = True                         # after a restart, outputs may still be on from before
        self._stop = threading.Event()
        if self.watchdog_s > 0:
            threading.Thread(target=self._watch, name="labrat-watchdog", daemon=True).start()

    def authenticate(self, header):
        who = super().authenticate(header)
        self.last_seen = time.monotonic()
        return who

    def dispatch(self, m: Message, who: Principal) -> dict:
        reply = super().dispatch(m, who)
        if m.op in ("set", "call"):
            self.armed = True
        elif m.op == "stop":
            self.armed = False
        return reply

    def god(self, m: Message, who: Principal) -> bool:
        """A token of the rat's own in god mode, or the nest passing on god mode where this rat allows it."""
        if who.god:
            return True
        return who is self.nest and m.args.get("god") is True and self.allow_god_mode

    def health(self, challenge: str | None = None) -> dict:
        h = {**super().health(), "fingerprint": self.fingerprint, "watchdog_s": self.watchdog_s}
        if challenge:                             # proves this rat holds the key without sending it
            h["proof"] = proof(self.key, challenge)
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
