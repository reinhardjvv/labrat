"""Experiments: each person's work in their own folder, with the settings and commands that produced it.

    [experiments]
    store = "nas"                # the store experiments go into (see store.py)
    snapshot = true              # save every device's identity, description and readings at start and stop

A person starts an experiment, works, and stops it. While it is open:

    <store>/<person>/<date>_<experiment>/
        experiment.json          who, when, what it is for, notes, and how it ended
        instruments.json         every device's identity, description and readings, at start and at stop
        log.jsonl                every command the person (or a token acting for them) sent
        <recording>-<time>.csv   the recordings they started, each with its .meta.json

The person is the token's name, or the person a token acts for (for = "<person>" in [[tokens]]). An AI agent
with its own token for a person therefore works in that person's folder, and every file says which token did
what. Each person has at most one experiment open. Open experiments survive a restart of the nest.
People read their own folders through the files operation; admins read everyone's.
"""
from __future__ import annotations

import base64
import json
import logging
import threading
import time
from pathlib import Path, PurePosixPath

from .config import check_name
from .message import LabratError, Message

log = logging.getLogger("labrat")
KEYS = {"store", "snapshot"}
READ_DEFAULT = 256 << 10
READ_MAX = 4 << 20


def _now(t: float | None = None) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(t))


class Experiment:
    def __init__(self, name: str, person: str, started_by: str, store: str, folder: str, description: str = "",
                 started: float | None = None, notes: list | None = None):
        self.name, self.person, self.started_by, self.store, self.folder = name, person, started_by, store, folder
        self.description = description
        self.started = started or time.time()
        self.notes: list[dict] = list(notes or [])

    def to_dict(self) -> dict:
        return {"experiment": self.name, "person": self.person, "started_by": self.started_by, "store": self.store,
                "folder": self.folder, "description": self.description, "started": _now(self.started),
                "_t": self.started, "notes": self.notes}

    @classmethod
    def from_dict(cls, d: dict) -> Experiment:
        return cls(d["experiment"], d["person"], d["started_by"], d["store"], d["folder"], d.get("description", ""),
                   d.get("_t"), d.get("notes"))


class Experiments:
    def __init__(self, hub, stores: dict, cfg: dict, state_file: Path):
        if not isinstance(cfg, dict):
            raise LabratError("bad_request", "[experiments] is a table")
        unknown = set(cfg) - KEYS
        if unknown:
            raise LabratError("bad_request", f"[experiments]: unknown setting(s) {', '.join(sorted(unknown))}")
        if cfg.get("store") not in stores:
            raise LabratError("bad_request", f"[experiments] store is one of {', '.join(stores) or 'none (add a [stores.<name>] table)'}")
        self.hub, self.stores = hub, stores
        self.store = stores[cfg["store"]]
        self.snapshot_on = bool(cfg.get("snapshot", True))
        self.state_file = state_file
        self._lock = threading.Lock()
        self.active: dict[str, Experiment] = {}                     # person -> the experiment they have open
        if state_file.is_file():
            for person, d in json.loads(state_file.read_text()).items():
                self.active[person] = Experiment.from_dict(d)
                self.store.opened(self._path(self.active[person], "log.jsonl"))
                log.info("experiment %s of %s is still open", d["experiment"], person)

    # -- files of an experiment -----------------------------------------------------------------------------
    def _path(self, exp: Experiment, name: str) -> Path:
        return self.store.root() / exp.folder / name

    def _write(self, exp: Experiment, name: str, value) -> None:
        p = self._path(exp, name)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + ".part")
        tmp.write_text(json.dumps(value, indent=2, default=str) + "\n", encoding="utf-8")
        tmp.replace(p)

    def _save(self) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps({p: e.to_dict() for p, e in self.active.items()}, indent=2))
        tmp.replace(self.state_file)

    def _record(self, exp: Experiment, **extra) -> dict:
        d = {k: v for k, v in exp.to_dict().items() if not k.startswith("_")}
        if self.hub.recorder is not None:
            d["recordings"] = [r.status() for r in self.hub.recorder.runs.values() if r.experiment is exp]
        return {**d, **extra}

    def snapshot(self, who) -> dict:
        """Every device's identity, description and readings now. A device that does not answer is noted."""
        out: dict[str, dict] = {}
        for e in self.hub.list_devices():
            dev = e["device"]
            if dev.endswith("/*"):
                out[dev] = {"error": "rat offline"}
                continue
            entry: dict = {"identity": {k: v for k, v in e.items() if k not in ("device", "locked_by")}}
            r = self.hub.handle({"op": "describe", "device": dev}, who)
            if not r.get("ok"):
                out[dev] = {**entry, "error": r["error"]["message"]}
                continue
            entry["description"] = r["value"]
            readings = {}
            for name, spec in (r["value"].get("attributes") or {}).items():
                for ch in spec.get("channels") or [None]:
                    g = self.hub.handle({"op": "get", "device": dev, "name": name, **({"channel": ch} if ch else {})}, who)
                    readings[f"{name}[{ch}]" if ch else name] = g.get("value") if g.get("ok") else {"error": g["error"]["code"]}
            entry["readings"] = readings
            out[dev] = entry
        return {"time": _now(), "devices": out}

    # -- the operation --------------------------------------------------------------------------------------
    def op(self, m: Message, who) -> dict | list:
        action = m.args.get("action") or ("status" if m.name else "list")
        if action == "list":
            return self.listing(self._person(who, m.args.get("person"), allow_all=True))
        if action == "start":
            if not m.name:
                raise LabratError("bad_request", "start needs the experiment's name")
            return self.start(m.name, who, str(m.args.get("description", "")))
        if action == "stop":
            return self.stop(who, m.name)
        if action == "note":
            return self.note(who, str(m.args.get("text", "")))
        if action == "status":
            exp = self.active.get(who.person)
            if exp is not None and (m.name in (None, exp.name)):
                return self._record(exp, state="open")
            if m.name is None:
                raise LabratError("not_found", f"{who.person} has no experiment open")
            found = [e for e in self.listing(who.person) if e["experiment"] == m.name]
            if not found:
                raise LabratError("not_found", f"{who.person} has no experiment {m.name!r}")
            return found[0]
        raise LabratError("bad_request", "args.action is list, start, stop, note or status")

    def _person(self, who, person, allow_all: bool = False) -> str:
        if not person or person == who.person:
            return who.person
        if who.role != "admin":
            raise LabratError("forbidden", f"{who.name} reads only {who.person}'s experiments")
        if person == "*" and allow_all:
            return "*"
        check_name(str(person), "person")
        return str(person)

    def start(self, name: str, who, description: str = "") -> dict:
        check_name(name, "experiment")
        check_name(who.person, "person")                          # the person's name is a folder name
        with self._lock:
            if who.person in self.active:
                raise LabratError("locked", f"{who.person} already has experiment {self.active[who.person].name} open; stop it first")
            day = time.strftime("%Y-%m-%d")
            folder, n = f"{who.person}/{day}_{name}", 2
            while self.store.find(Path(folder)) is not None:      # data is never overwritten
                folder, n = f"{who.person}/{day}_{name}-{n}", n + 1
            exp = Experiment(name, who.person, who.name, self.store.name, folder, description)
            try:
                self._write(exp, "experiment.json", self._record(exp, state="open"))
            except OSError as e:
                raise LabratError("unavailable", f"store {self.store.name}: {e}") from None
            self.active[who.person] = exp
            self.store.opened(self._path(exp, "log.jsonl"))
            self._save()
        if self.snapshot_on:
            self._write(exp, "instruments.json", {"start": self.snapshot(who)})
        log.info("experiment %s started by %s in %s", name, who.name, folder)
        return self._record(exp, state="open")

    def stop(self, who, name: str | None = None) -> dict:
        exp = self.active.get(who.person)
        if exp is None or name not in (None, exp.name):
            raise LabratError("not_found", f"{who.person} has no experiment {name or ''} open".replace("  ", " "))
        if self.hub.recorder is not None:
            self.hub.recorder.stop_all(exp)
        if self.snapshot_on:
            p = self._path(exp, "instruments.json")
            before = json.loads(p.read_text()) if p.is_file() else {}
            self._write(exp, "instruments.json", {**before, "end": self.snapshot(who)})
        record = self._record(exp, state="ended", ended=_now(), stopped_by=who.name)
        self._write(exp, "experiment.json", record)
        self.log(who, {"time": _now(), "who": who.name, "role": who.role, **({"for": who.for_} if who.for_ else {}),
                       "op": "experiment", "args": {"action": "stop"}, "ok": True})    # the audit log comes after it closes
        with self._lock:
            self.active.pop(who.person, None)
            self._save()
        self.store.closed(self._path(exp, "log.jsonl"))
        log.info("experiment %s of %s stopped by %s", exp.name, exp.person, who.name)
        return record

    def note(self, who, text: str) -> dict:
        exp = self.active.get(who.person)
        if exp is None:
            raise LabratError("not_found", f"{who.person} has no experiment open")
        if not text.strip():
            raise LabratError("bad_request", "a note needs args.text")
        exp.notes.append({"time": _now(), "by": who.name, "text": text})
        self._write(exp, "experiment.json", self._record(exp, state="open"))
        with self._lock:
            self._save()
        return self._record(exp, state="open")

    def log(self, who, entry: dict) -> None:
        """Called for every audited command: it goes into the sender's open experiment as well."""
        exp = self.active.get(who.person)
        if exp is None:
            return
        try:
            p = self._path(exp, "log.jsonl")
            p.parent.mkdir(parents=True, exist_ok=True)
            with open(p, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, default=str) + "\n")
        except (OSError, LabratError) as e:
            log.error("experiment %s: cannot write its log: %s", exp.name, getattr(e, "message", e))

    def listing(self, person: str) -> list[dict]:
        people = [person]
        if person == "*":
            people = sorted(e["name"] for e in self.store.listing(Path(".")) if e["folder"])
        out = []
        for p in people:
            for e in self.store.listing(Path(p)):
                f = self.store.find(Path(p) / e["name"] / "experiment.json") if e["folder"] else None
                if f is None:
                    continue
                try:
                    d = json.loads(f.read_text())
                except (OSError, ValueError):
                    continue
                d.pop("recordings", None)
                d.pop("notes", None)
                out.append(d)
        return sorted(out, key=lambda d: d.get("started", ""), reverse=True)

    # -- reading files back ---------------------------------------------------------------------------------
    def resolve(self, path: str | None, who, person: str | None = None) -> Path:
        """The file or folder a person may read, at a path inside their own folder."""
        owner = self._person(who, person)
        rel = PurePosixPath(path or ".")
        if rel.is_absolute() or ".." in rel.parts:
            raise LabratError("bad_request", "a path is relative to your folder and has no '..'")
        found = self.store.find(Path(owner) / rel)
        if found is None:
            raise LabratError("not_found", f"no {path or 'folder'} for {owner}" if path else f"{owner} has no experiments yet")
        own = found.parents[len(rel.parts) - 1] if rel.parts else found       # <buffer or store>/<owner>
        if not found.resolve().is_relative_to(own.resolve()):
            raise LabratError("forbidden", f"{path} leads outside {owner}'s folder")
        return found

    def files(self, m: Message, who) -> dict:
        owner = self._person(who, m.args.get("person"))
        p = self.resolve(m.name, who, m.args.get("person"))
        rel = PurePosixPath(m.name or ".")
        if p.is_dir():
            return {"person": owner, "path": str(rel), "files": self.store.listing(Path(owner) / rel)}
        offset = int(m.args.get("offset", 0))
        length = min(int(m.args.get("length", READ_DEFAULT)), READ_MAX)
        if offset < 0 or length <= 0:
            raise LabratError("bad_request", "offset is 0 or more and length is positive")
        size = p.stat().st_size
        with open(p, "rb") as f:
            f.seek(offset)
            data = f.read(length)
        out = {"person": owner, "path": str(rel), "size": size, "offset": offset, "length": len(data),
               "more": offset + len(data) < size}
        for cut in range(4):                                    # a read may end inside a multi-byte character
            try:
                text = data[:len(data) - cut].decode("utf-8")
            except UnicodeDecodeError:
                continue
            out.update(text=text, length=len(data) - cut, more=offset + len(data) - cut < size)
            return out
        out["base64"] = base64.b64encode(data).decode()
        return out

    def close(self) -> None:
        """Open experiments stay open in the state file and carry on when the nest starts again."""
        with self._lock:
            self._save()
