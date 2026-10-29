"""Recordings: readings, measurements and streams written to storage on a schedule, formatted for you.

    [stores.lab]
    path = "/srv/labrat-data"              # a local folder or a mounted network share (see store.py)

    [recordings.supply-log]
    store = "lab"
    device = "bench-rat/psu"
    read = ["VoltageLevel", "OutputEnabled"]   # attributes, on every channel they have
    every_s = 1.0
    format = "csv"                         # csv or jsonl
    start = true                           # start with the nest; otherwise start it with 'labrat record start'

    [recordings.scope-traces]
    store = "lab"
    device = "scope"
    call = "Waveform"                      # a function, such as a measurement or a waveform fetch
    every_s = 10
    format = "jsonl"

    [recordings.camera]
    store = "lab"
    device = "optics-rat/cam"
    stream = "Video"                       # a stream, written as it comes
    duration_s = 60

Each run writes to <store>/<recording>/<date>/<recording>-<time>.<ext>, next to a <same name>.meta.json file
that holds the device's description with its units, the settings of the recording, who started it, and when
it started and ended. Every reading carries an ISO 8601 time and a Unix time. A run ends after duration_s or
count readings, when it is stopped, or when the nest stops. A recording started by someone with an experiment
open goes into that experiment's folder instead, with no store needed (see experiment.py).
"""
from __future__ import annotations

import csv
import json
import logging
import threading
import time

from .config import check_name
from .message import LabratError, Message

log = logging.getLogger("labrat")
FORMATS = ("csv", "jsonl")
KEYS = {"store", "device", "read", "call", "args", "channel", "stream", "every_s", "duration_s", "count", "format", "start"}


def check_spec(name: str, spec: dict, stores: dict, need_store: bool = True) -> dict:
    """A recording's settings with defaults, or LabratError naming what is wrong."""
    check_name(name, "recording")
    spec = dict(spec)
    unknown = set(spec) - KEYS
    if unknown:
        raise LabratError("bad_request", f"recording {name}: unknown setting(s) {', '.join(sorted(unknown))}")
    if (need_store or spec.get("store") is not None) and spec.get("store") not in stores:
        raise LabratError("bad_request", f"recording {name}: store is one of {', '.join(stores) or 'none (add a [stores.<name>] table)'}")
    if not spec.get("device"):
        raise LabratError("bad_request", f"recording {name} needs a device")
    what = [k for k in ("read", "call", "stream") if spec.get(k)]
    if len(what) != 1:
        raise LabratError("bad_request", f"recording {name} needs exactly one of read, call or stream")
    if spec.get("read") is not None and not isinstance(spec["read"], list):
        spec["read"] = [spec["read"]]
    spec.setdefault("format", "jsonl" if spec.get("call") else "csv")
    if spec["format"] not in FORMATS:
        raise LabratError("bad_request", f"recording {name}: format is {' or '.join(FORMATS)}")
    if not spec.get("stream"):
        every = float(spec.setdefault("every_s", 1.0))
        if not 0.01 <= every <= 86400:
            raise LabratError("bad_request", f"recording {name}: every_s is between 0.01 and 86400")
    for k in ("duration_s", "count"):
        if spec.get(k) is not None and float(spec[k]) <= 0:
            raise LabratError("bad_request", f"recording {name}: {k} must be positive")
    return spec


class Run:
    """One running recording, in its own thread."""

    def __init__(self, hub, name: str, spec: dict, store, who, experiment=None):
        self.hub, self.name, self.spec, self.who, self.store, self.experiment = hub, name, spec, who, store, experiment
        self.started = time.time()
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(self.started))
        if experiment is not None:
            folder = store.root() / experiment.folder
        else:
            folder = store.root() / name / time.strftime("%Y-%m-%d", time.localtime(self.started))
        folder.mkdir(parents=True, exist_ok=True)
        ext = {"csv": "csv", "jsonl": "jsonl"}[spec["format"]] if not spec.get("stream") else "data"
        self.path = folder / f"{name}-{stamp}.{ext}"
        self.meta_path = folder / f"{name}-{stamp}.meta.json"
        self.readings = 0
        self.error: str | None = None
        self.stopping = threading.Event()
        self.thread = threading.Thread(target=self._run, name=f"labrat-record-{name}", daemon=True)

    def start(self) -> Run:
        self._meta()
        self.store.opened(self.path)
        self.thread.start()
        return self

    def stop(self) -> None:
        self.stopping.set()

    @property
    def running(self) -> bool:
        return self.thread.is_alive()

    def status(self) -> dict:
        return {"recording": self.name, "running": self.running, "file": str(self.path), "readings": self.readings,
                "started": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(self.started)), "started_by": self.who.name,
                **({"experiment": self.experiment.name} if self.experiment else {}), **({"error": self.error} if self.error else {})}

    # -- the work -------------------------------------------------------------------------------------------
    def _ask(self, msg: dict):
        with self.hub.recording():
            reply = self.hub.handle(msg, self.who)
        if not reply.get("ok"):
            raise LabratError(reply["error"]["code"], reply["error"]["message"])
        return reply

    def _meta(self, ended: float | None = None) -> None:
        try:
            described = self._ask({"op": "describe", "device": self.spec["device"]})["value"]
        except LabratError as e:
            described = {"error": e.message}
        meta = {"recording": self.name, "settings": self.spec, "device": self.spec["device"], "description": described,
                "file": self.path.name, "started": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(self.started)),
                "started_by": self.who.name, "readings": self.readings}
        if self.who.person != self.who.name:
            meta["for"] = self.who.person
        if self.experiment is not None:
            meta["experiment"] = self.experiment.name
        if ended:
            meta["ended"] = time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(ended))
        if self.error:
            meta["error"] = self.error
        self.meta_path.write_text(json.dumps(meta, indent=2, default=str) + "\n", encoding="utf-8")

    def _columns(self) -> list[tuple[str, str | None]]:
        d = self._ask({"op": "describe", "device": self.spec["device"]})["value"]
        attrs = d.get("attributes") or {}
        cols = []
        for n in self.spec["read"]:
            if n not in attrs:
                raise LabratError("not_found", f"{self.spec['device']} has no attribute {n!r}")
            chs = attrs[n].get("channels")
            if self.spec.get("channel"):
                chs = [str(self.spec["channel"])]
            cols += [(n, c) for c in (chs or [None])]
        return cols

    def _sample(self, cols):
        if self.spec.get("read"):
            out = {}
            for n, c in cols:
                msg = {"op": "get", "device": self.spec["device"], "name": n, **({"channel": c} if c else {})}
                out[f"{n}[{c}]" if c else n] = self._ask(msg).get("value")
            return out
        msg = {"op": "call", "device": self.spec["device"], "name": self.spec["call"], "args": self.spec.get("args") or {},
               **({"channel": str(self.spec["channel"])} if self.spec.get("channel") else {})}
        return {"value": self._ask(msg).get("value")}

    def _run(self) -> None:
        try:
            if self.spec.get("stream"):
                self._stream()
            else:
                self._poll()
        except LabratError as e:
            self.error = f"{e.code}: {e.message}"
            log.warning("recording %s stopped: %s", self.name, self.error)
        except Exception as e:
            self.error = f"internal: {e}"
            log.exception("recording %s", self.name)
        finally:
            self.store.closed(self.path)
            self._meta(time.time())

    def _done(self) -> bool:
        s = self.spec
        if self.stopping.is_set():
            return True
        if s.get("duration_s") and time.time() - self.started >= float(s["duration_s"]):
            return True
        return bool(s.get("count") and self.readings >= int(s["count"]))

    def _poll(self) -> None:
        cols = self._columns() if self.spec.get("read") else []
        every = float(self.spec["every_s"])
        with open(self.path, "w", newline="", encoding="utf-8") as f:
            writer = None
            nxt = time.monotonic()
            while not self._done():
                t = time.time()
                row = {"time": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(t)) + f".{int(t % 1 * 1000):03d}",
                       "unix_s": round(t, 3), **self._sample(cols)}
                if self.spec["format"] == "jsonl":
                    f.write(json.dumps(row, default=str) + "\n")
                else:
                    if writer is None:
                        writer = csv.DictWriter(f, fieldnames=list(row))
                        writer.writeheader()
                    writer.writerow({k: json.dumps(v) if isinstance(v, (list, dict)) else v for k, v in row.items()})
                f.flush()
                self.readings += 1
                nxt += every
                self.stopping.wait(max(0.0, nxt - time.monotonic()))

    def _stream(self) -> None:
        kind, chunks = self.hub.open_stream(self.spec["device"], self.spec["stream"], self.spec.get("channel"), self.who)
        ext = ".mjpeg" if "x-mixed-replace" in kind else ".jsonl" if "ndjson" in kind else ".bin"
        self.store.closed(self.path)
        self.path = self.path.with_suffix(ext)
        self.store.opened(self.path)
        self._meta()
        try:
            with open(self.path, "wb") as f:
                for chunk in chunks:
                    f.write(chunk)
                    self.readings += 1
                    if self._done():
                        break
        finally:
            close = getattr(chunks, "close", None)
            if close:
                close()


class Recorder:
    """The recordings a nest or rat knows, and the runs going now."""

    def __init__(self, hub, stores: dict, recordings: dict):
        self.hub = hub
        self.stores = stores                                          # name -> Store
        self.specs = {n: check_spec(n, s, self.stores) for n, s in (recordings or {}).items()}
        self.runs: dict[str, Run] = {}
        self._lock = threading.Lock()

    def autostart(self, who) -> None:
        for name, spec in self.specs.items():
            if spec.get("start"):
                try:
                    self.start(name, who)
                except LabratError as e:
                    log.warning("recording %s did not start: %s", name, e.message)

    def start(self, name: str, who, spec: dict | None = None) -> dict:
        with self._lock:
            if name in self.runs and self.runs[name].running:
                raise LabratError("locked", f"recording {name} is already running")
            exps = getattr(self.hub, "experiments", None)
            experiment = exps.active.get(who.person) if exps is not None else None
            if spec is not None:
                spec = check_spec(name, spec, self.stores, need_store=experiment is None)
            elif name in self.specs:
                spec = self.specs[name]
            else:
                raise LabratError("not_found", f"no recording {name!r}; there is {', '.join(self.specs) or 'none'}")
            store = self.stores[experiment.store] if experiment is not None else self.stores[spec["store"]]
            try:
                run = Run(self.hub, name, spec, store, who, experiment).start()
            except OSError as e:
                raise LabratError("unavailable", f"store {store.name}: {e}") from None
            self.runs[name] = run
        log.info("recording %s started by %s into %s", name, who.name, run.path)
        return run.status()

    def stop(self, name: str) -> dict:
        run = self.runs.get(name)
        if run is None:
            raise LabratError("not_found", f"recording {name!r} has not run")
        run.stop()
        run.thread.join(timeout=10)
        return run.status()

    def stop_all(self, experiment=None) -> None:
        """Every run, or those of one experiment."""
        runs = [r for r in self.runs.values() if experiment is None or r.experiment is experiment]
        for run in runs:
            run.stop()
        for run in runs:
            run.thread.join(timeout=10)

    def listing(self) -> dict:
        return {"stores": {n: s.status() for n, s in self.stores.items()},
                "recordings": [{"recording": n, **{k: v for k, v in s.items() if k != "start"},
                                **({"run": self.runs[n].status()} if n in self.runs else {})} for n, s in self.specs.items()]
                + [{"recording": n, "run": r.status()} for n, r in self.runs.items() if n not in self.specs]}

    def op(self, m: Message, who) -> dict:
        action = m.args.get("action", "status")
        if m.name is None:
            return self.listing()
        if action == "start":
            spec = {k: v for k, v in m.args.items() if k != "action"} or None
            return self.start(m.name, who, spec)
        if action == "stop":
            return self.stop(m.name)
        if action == "status":
            if m.name in self.runs:
                return self.runs[m.name].status()
            raise LabratError("not_found", f"recording {m.name!r} has not run")
        raise LabratError("bad_request", "args.action is start, stop or status")
