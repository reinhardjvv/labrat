"""Jobs: work a person sends to the lab's workers, queued until workers that fit are free.

    labrat job run fit.py --gpus 1                     # anywhere that has a free GPU and takes your jobs
    labrat job run train.py --nodes 2 --exclusive      # two workers at once, started together
    labrat job run sweep.py --on gpu-box -- --steps 40
    labrat jobs                                        # yours, newest first
    labrat job output 12 -f

A job is a script (sent with it, or one already on the worker), its arguments and what it needs: CPUs, memory,
GPUs, a number of workers, devices it may change, and a time limit. The nest places it on workers that take the
person's jobs (workers.py), are taking work now, have a free slot and fit what it needs, and queues it until
they do. A job across several workers starts only when all of them are free, every part learns its rank and the
leader's address, and when one part fails the others are stopped.

Each part runs with a token of its own that the nest and the worker both derive from the worker's key, so it never
travels. The token acts as the person who sent the job, with that person's limits, locks and approval rules, but
may change only the devices the job names, and may not start other jobs, change people or workers, or approve
anything. It stops working when the part ends.
"""
from __future__ import annotations

import base64
import binascii
import json
import logging
import os
import re
import time
from collections import deque
from pathlib import Path, PurePosixPath

from . import signing
from .config import check_name
from .hub import Principal, token_hash
from .message import LabratError, Message, role_allows

log = logging.getLogger("labrat")
BUNDLE_MAX = 16 << 20
OUTPUT_LINES = 5000
ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
RESERVED_ENV = {"PATH", "HOME", "USER", "LOGNAME", "SHELL", "TMPDIR", "MASTER_ADDR", "MASTER_PORT", "RANK", "WORLD_SIZE",
                "LOCAL_RANK", "CUDA_VISIBLE_DEVICES", "PYTHONPATH", "LD_PRELOAD", "LD_LIBRARY_PATH"}
ENDED = ("done", "failed", "stopped", "cancelled")
LOST_S = 30                         # a part its worker no longer reports for this long has been lost


def _now(t: float | None = None) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(t))


def part_token(key: str, job: int, rank: int) -> str:
    """The token of one part of a job, derived from its worker's key on both sides."""
    return "job." + signing.mac(key, "job", str(job), str(rank))


def check_spec(spec: dict, devices_exist) -> dict:
    """A job's specification, checked and with only the fields LABRAT knows."""
    known = {"name", "script", "files", "args", "on", "needs", "exclusive", "devices", "env", "minutes"}
    unknown = set(spec) - known
    if unknown:
        raise LabratError("bad_request", f"unknown job setting(s) {', '.join(sorted(unknown))}")
    script = str(spec.get("script") or "")
    if not script or "/" in script or "\\" in script or script.startswith("."):
        raise LabratError("bad_request", "a job needs script, a file name such as fit.py")
    files = spec.get("files") or {}
    if not isinstance(files, dict):
        raise LabratError("bad_request", "files maps file names to their contents in base64")
    size = 0
    for name, data in files.items():
        if not isinstance(name, str) or not name or "/" in name or "\\" in name or name.startswith(".") or len(name) > 255:
            raise LabratError("bad_request", f"file name {name!r}: a plain name, sent with the job beside its script")
        try:
            size += len(base64.b64decode(str(data), validate=True))
        except (binascii.Error, ValueError):
            raise LabratError("bad_request", f"{name} is not base64") from None
    if size > BUNDLE_MAX:
        raise LabratError("bad_request", f"the job's files come to {size >> 20} MB; at most {BUNDLE_MAX >> 20} MB travel with a job")
    if files and script not in files:
        raise LabratError("bad_request", f"{script} is not among the files sent with the job")
    args = spec.get("args") or []
    if not isinstance(args, list) or len(args) > 256 or not all(isinstance(a, str) and len(a) <= 4096 for a in args):
        raise LabratError("bad_request", "args is a list of strings")
    on = spec.get("on") or []
    if not isinstance(on, list) or not all(isinstance(w, str) for w in on):
        raise LabratError("bad_request", "on is a list of worker names")
    needs = spec.get("needs") or {}
    if not isinstance(needs, dict) or set(needs) - {"cpus", "memory_gb", "gpus", "gpu_memory_gb", "nodes"}:
        raise LabratError("bad_request", "needs takes cpus, memory_gb, gpus, gpu_memory_gb and nodes")
    for k, v in needs.items():
        if not isinstance(v, (int, float)) or isinstance(v, bool) or v < 0:
            raise LabratError("bad_request", f"needs.{k} is a number of 0 or more")
    nodes = int(needs.get("nodes") or len(on) or 1)
    if not 1 <= nodes <= 256:
        raise LabratError("bad_request", "a job runs on 1 to 256 workers")
    if on and nodes > len(on):
        raise LabratError("bad_request", f"the job needs {nodes} workers and names {len(on)}")
    devices = spec.get("devices") or []
    if not isinstance(devices, list) or not all(isinstance(d, str) for d in devices):
        raise LabratError("bad_request", "devices is a list of device ids the job may change")
    for d in devices:
        devices_exist(d)
    env = spec.get("env") or {}
    if not isinstance(env, dict) or len(env) > 64:
        raise LabratError("bad_request", "env maps up to 64 variable names to values")
    for k, v in env.items():
        if not ENV_NAME.match(str(k)) or k in RESERVED_ENV or str(k).startswith("LABRAT_") or not isinstance(v, str) or len(v) > 4096:
            raise LabratError("bad_request", f"env {k!r}: a plain variable name not set by LABRAT itself, with a string value")
    minutes = spec.get("minutes")
    if minutes is not None and (not isinstance(minutes, (int, float)) or not 0 < minutes <= 7 * 24 * 60):
        raise LabratError("bad_request", "minutes, the time limit, is between 0 and 10080")
    out = {"name": str(spec.get("name") or PurePosixPath(script).stem.replace(".", "-")), "script": script, "files": files,
           "args": args, "on": on, "needs": {**{k: v for k, v in needs.items() if k != "nodes"}, "nodes": nodes},
           "exclusive": bool(spec.get("exclusive")), "devices": devices, "env": env}
    check_name(out["name"], "job")
    if minutes is not None:
        out["minutes"] = float(minutes)
    return out


class Jobs:
    def __init__(self, hub, workers, state: Path):
        self.hub, self.workers = hub, workers
        self.dir = state / "jobs"
        self.jobs: dict[int, dict] = {}
        self._lock = workers._lock                  # one lock for workers and jobs, so placement sees both at once
        self._next = 1
        if self.dir.is_dir():
            for p in self.dir.glob("*.json"):
                try:
                    j = json.loads(p.read_text())
                except (OSError, ValueError):
                    continue
                self._next = max(self._next, int(j["id"]) + 1)
                if j["status"] not in ENDED:                         # the nest stopped while it waited or ran
                    j.update(status="failed", error="the nest restarted while the job was queued or running", ended=_now())
                    for part in j["parts"]:
                        if part["status"] not in ENDED:
                            part["status"] = "failed"
                    self._write(j)
                self.jobs[j["id"]] = j

    # -- records --------------------------------------------------------------------------------------------
    def _write(self, j: dict) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        rec = {k: v for k, v in j.items() if not k.startswith("_")}
        rec["spec"] = {k: v for k, v in j["spec"].items() if k != "files"}
        rec["spec"]["files"] = sorted(j["spec"].get("files") or {})
        rec["parts"] = [{k: v for k, v in p.items() if not k.startswith("_")} for p in j["parts"]]
        tmp = self.dir / f"{j['id']}.tmp"
        tmp.write_text(json.dumps(rec, indent=2, default=str))
        tmp.replace(self.dir / f"{j['id']}.json")

    def public(self, j: dict, full: bool = False) -> dict:
        out = {k: j[k] for k in ("id", "name", "person", "by", "status", "submitted") if k in j}
        for k in ("started", "ended", "error", "exit_code"):
            if j.get(k) is not None:
                out[k] = j[k]
        out["workers"] = [p["worker"] for p in j["parts"] if p.get("worker")]
        out["nodes"] = j["spec"]["needs"]["nodes"]
        if full:
            out["spec"] = {k: v for k, v in j["spec"].items() if k != "files"}
            out["spec"]["files"] = sorted(j["spec"].get("files") or {})
            out["parts"] = [{k: v for k, v in p.items() if not k.startswith("_")} for p in j["parts"]]
        return out

    # -- the operation --------------------------------------------------------------------------------------
    def op(self, m: Message, who: Principal):
        action = m.args.get("action", "list")
        if action == "submit":
            if who.job is not None:
                raise LabratError("forbidden", "a job may not start other jobs")
            if not role_allows(who.role, "set"):
                raise LabratError("forbidden", f"{who.name} ({who.role}) may not start jobs")
            return self.submit(m.args.get("spec") or {}, who)
        if action == "list":
            person = m.args.get("person")
            if person and person != who.person and who.role != "admin":
                raise LabratError("forbidden", f"{who.name} sees only {who.person}'s jobs")
            with self._lock:
                rows = [self.public(j) for j in self.jobs.values()
                        if (person == "*" and who.role == "admin") or j["person"] == (person or who.person)]
            return sorted(rows, key=lambda r: -r["id"])[:int(m.args.get("limit", 100))]
        j = self._get(m.name, who)
        if action == "status":
            with self._lock:
                return self.public(j, full=True)
        if action == "output":
            return self.output(j, int(m.args.get("rank", 0)), int(m.args.get("since", 0)))
        if action == "stop":
            if who.job is not None and who.job != j["id"]:
                raise LabratError("forbidden", "a job may stop only itself")
            if not role_allows(who.role, "set"):
                raise LabratError("forbidden", f"{who.name} ({who.role}) may not stop jobs")
            return self.stop(j, who.name)
        raise LabratError("bad_request", "args.action is submit, list, status, output or stop")

    def _get(self, name, who) -> dict:
        try:
            j = self.jobs[int(name)]
        except (TypeError, ValueError, KeyError):
            raise LabratError("not_found", f"no job {name!r}") from None
        if j["person"] != who.person and who.role != "admin":
            raise LabratError("forbidden", f"job {j['id']} is {j['person']}'s")
        return j

    def submit(self, spec: dict, who: Principal) -> dict:
        spec = check_spec(spec, self.hub.exists)
        person = who.person
        for w in spec["on"]:
            if w not in self.workers.entries:
                raise LabratError("not_found", f"no worker {w!r}")
            if not self.workers.allows(w, person):
                raise LabratError("forbidden", f"{w} does not take {person}'s jobs")
        if not spec["on"] and not any(self.workers.allows(w, person) for w in self.workers.entries):
            raise LabratError("not_found", f"no worker takes {person}'s jobs yet (labrat worker join on a computer of yours)")
        with self._lock:
            jid = self._next
            self._next += 1
            n = spec["needs"]["nodes"]
            j = {"id": jid, "name": spec["name"], "person": person, "by": who.name, "for": who.for_, "role": who.role,
                 "status": "queued", "submitted": _now(), "spec": spec, "stop": False,
                 "parts": [{"rank": r, "worker": None, "status": "queued"} for r in range(n)]}
            self.jobs[jid] = j
            self._write(j)
            self._place()
            self.workers.changed.notify_all()
        log.info("job %d (%s) from %s: %s", jid, spec["name"], who.name, j["status"])
        return self.public(j, full=True)

    def stop(self, j: dict, by: str) -> dict:
        with self._lock:
            if j["status"] == "queued":
                j.update(status="cancelled", ended=_now(), stopped_by=by)
                for p in j["parts"]:
                    p["status"] = "cancelled"
            elif j["status"] not in ENDED:
                j["stop"], j["stopped_by"] = True, by
            self._write(j)
            self.workers.changed.notify_all()
        return self.public(j)

    def output(self, j: dict, rank: int, since: int = 0) -> dict:
        if not 0 <= rank < len(j["parts"]):
            raise LabratError("not_found", f"job {j['id']} has ranks 0 to {len(j['parts']) - 1}")
        part = j["parts"][rank]
        lines = list(part.get("_out") or [])
        first = part.get("_dropped", 0)
        start = max(since - first, 0)
        return {"job": j["id"], "rank": rank, "status": part["status"], "from": first + start,
                "next": first + len(lines), "text": "\n".join(lines[start:])}

    # -- placing jobs on workers ----------------------------------------------------------------------------
    def _active(self, worker: str) -> list[dict]:
        return [p for j in self.jobs.values() if j["status"] == "running" for p in j["parts"]
                if p.get("worker") == worker and p["status"] in ("starting", "running")]

    def _fits(self, j: dict, worker: str) -> bool:
        w = self.workers
        r = w.report(worker)
        if not (w.online(worker) and w.allows(worker, j["person"]) and r.get("accepting", True)):
            return False
        active = self._active(worker)
        if len(active) >= int(r.get("slots", 1)):
            return False
        spec = j["spec"]
        if spec["files"] and r.get("accept") == "scripts":
            return False
        if r.get("backend", "local") != "local":                # a scheduler there decides what fits
            return True
        needs, limits = spec["needs"], r.get("limits", {})
        gpus = r.get("gpus", [])
        if spec["exclusive"] and (active or r.get("gpu_busy")):
            return False
        if needs.get("cpus", 0) > min(limits.get("cpus") or r.get("cpus", 1), r.get("cpus", 1)):
            return False
        if needs.get("memory_gb", 0) > (limits.get("memory_gb") or r.get("memory_gb") or 0) and needs.get("memory_gb"):
            return False
        usable = [g for g in gpus if not g.get("busy")] if needs.get("gpus") else gpus
        if needs.get("gpus", 0) > min(len(usable), limits.get("gpus", len(gpus))):
            return False
        if needs.get("gpu_memory_gb") and not any(g["memory_gb"] >= needs["gpu_memory_gb"] for g in usable):
            return False
        return True

    def _order(self, j: dict, names: list[str]) -> list[str]:
        """Pinned workers in the order named; otherwise the person's own first, then idle ones, then the least loaded."""
        if j["spec"]["on"]:
            return [n for n in j["spec"]["on"] if n in names]
        r = self.workers.report

        def key(n):
            return (self.workers.entries[n]["owner"] != j["person"], not r(n).get("idle", False), r(n).get("load", 0) / max(r(n).get("cpus", 1), 1))
        return sorted(names, key=key)

    def _place(self) -> None:
        """Give queued jobs, oldest first, to workers that fit. Called with the lock held."""
        for j in sorted((j for j in self.jobs.values() if j["status"] == "queued"), key=lambda j: j["id"]):
            pool = j["spec"]["on"] or list(self.workers.entries)
            fit = self._order(j, [n for n in pool if self._fits(j, n)])
            n = j["spec"]["needs"]["nodes"]
            if len(fit) < n:
                continue
            chosen = fit[:n]
            leader = self.workers.report(chosen[0])
            leader_addr = "127.0.0.1" if n == 1 else (leader.get("link") or (leader.get("addresses") or ["127.0.0.1"])[0])
            for part, worker in zip(j["parts"], chosen, strict=True):
                part.update(worker=worker, status="starting", _since=time.monotonic(), _delivered=False)
            j.update(status="running", started=_now(), leader=leader_addr, port=int(leader.get("link_port", 29500)))
            for part in j["parts"]:
                e = self.workers.entries[part["worker"]]
                self.hub.tokens[token_hash(part_token(e["key"], j["id"], part["rank"]))] = Principal(
                    j["by"], "operator" if j["role"] == "admin" else j["role"], j.get("for"), job=j["id"], devices=tuple(j["spec"]["devices"]))
            self._write(j)
            log.info("job %d placed on %s", j["id"], ", ".join(chosen))

    def _payload(self, j: dict, part: dict) -> dict:
        spec = j["spec"]
        return {"job": j["id"], "rank": part["rank"], "world_size": len(j["parts"]), "name": j["name"], "person": j["person"],
                "by": j["by"], "script": spec["script"], "files": spec["files"], "args": spec["args"], "env": spec["env"],
                "needs": spec["needs"], "exclusive": spec["exclusive"], "devices": spec["devices"], "leader": j["leader"],
                "port": j["port"], **({"minutes": spec["minutes"]} if "minutes" in spec else {})}

    # -- the worker's side ----------------------------------------------------------------------------------
    def next_for(self, worker: str, wait: float, running: list) -> dict:
        deadline = time.monotonic() + wait
        held = {(int(r.get("job", -1)), int(r.get("rank", -1))) for r in running if isinstance(r, dict)}
        with self._lock:
            self._lost(worker, held)
            while True:
                self._place()
                parts = [(j, p) for j in self.jobs.values() if j["status"] == "running" for p in j["parts"]
                         if p.get("worker") == worker and p["status"] == "starting" and not p.get("_delivered")]
                stop = [{"job": j["id"], "rank": p["rank"]} for j in self.jobs.values() if j.get("stop") and j["status"] == "running"
                        for p in j["parts"] if p.get("worker") == worker and p["status"] in ("starting", "running")]
                left = deadline - time.monotonic()
                if parts or stop or left <= 0:
                    break
                self.workers.changed.wait(left)
            for _, p in parts:
                p["_delivered"] = True
                p["_since"] = time.monotonic()
            return {"parts": [self._payload(j, p) for j, p in parts], "stop": stop}

    def _lost(self, worker: str, held: set) -> None:
        """Parts this worker was given but no longer reports, after a restart of the worker, have failed."""
        for j in self.jobs.values():
            if j["status"] != "running":
                continue
            for p in j["parts"]:
                if (p.get("worker") == worker and p["status"] in ("starting", "running") and p.get("_delivered")
                        and (j["id"], p["rank"]) not in held and time.monotonic() - p.get("_since", 0) > LOST_S):
                    self._ended(j, p, None, f"{worker} no longer runs it")

    def from_worker(self, worker: str, action: str, obj: dict) -> dict:
        with self._lock:
            j = self.jobs.get(int(obj.get("job", -1)))
            rank = int(obj.get("rank", -1))
            if j is None or not 0 <= rank < len(j["parts"]) or j["parts"][rank].get("worker") != worker:
                raise LabratError("not_found", "no such part of a job on this worker")
            part = j["parts"][rank]
            if action == "started":
                if part["status"] == "starting":
                    part.update(status="running", started=_now())
                    self._write(j)
                return {"stop": bool(j.get("stop"))}
            if action == "output":
                out = part.setdefault("_out", deque(maxlen=OUTPUT_LINES))
                for line in str(obj.get("text", "")).splitlines():
                    if len(out) == out.maxlen:
                        part["_dropped"] = part.get("_dropped", 0) + 1
                    out.append(line[:4000])
                return {"stop": bool(j.get("stop"))}
            if action == "done":
                code = obj.get("code")
                self._ended(j, part, int(code) if isinstance(code, int) else None, str(obj.get("error") or "") or None,
                            obj.get("manifest"))
                return {}
        raise LabratError("bad_request", f"unknown worker action {action!r}")

    def _ended(self, j: dict, part: dict, code: int | None, error: str | None, manifest=None) -> None:
        if part["status"] in ENDED:
            return
        stopped = j.get("stop")
        part.update(status="stopped" if stopped else "done" if code == 0 else "failed", ended=_now(), exit_code=code)
        if error:
            part["error"] = error[:2000]
        if isinstance(manifest, dict):
            part["files"] = manifest
        e = self.workers.entries.get(part.get("worker") or "")
        if e is not None:
            self.hub.tokens.pop(token_hash(part_token(e["key"], j["id"], part["rank"])), None)
        if part["status"] == "failed" and not stopped and len(j["parts"]) > 1:
            j["stop"], j["stopped_by"] = True, f"rank {part['rank']} failed"         # the other parts cannot go on alone
        if all(p["status"] in ENDED for p in j["parts"]):
            codes = [p.get("exit_code") for p in j["parts"]]
            states = {p["status"] for p in j["parts"]}
            j["status"] = "failed" if "failed" in states else "stopped" if "stopped" in states else "done"
            j["ended"] = _now()
            j["exit_code"] = next((c for c in codes if c != 0), 0)
            errors = [f"rank {p['rank']}: {p['error']}" for p in j["parts"] if p.get("error")]
            if errors:
                j["error"] = "; ".join(errors)[:2000]
            log.info("job %d %s", j["id"], j["status"])
        self._write(j)
        self.workers.changed.notify_all()

    def worker_gone(self, name: str) -> None:
        with self._lock:
            for j in self.jobs.values():
                for p in j["parts"]:
                    if p.get("worker") == name and p["status"] in ("starting", "running"):
                        self._ended(j, p, None, f"{name} was removed")

    def running_on(self, worker: str) -> list[dict]:
        return [{"job": j["id"], "name": j["name"], "person": j["person"], "since": p.get("started") or j.get("started")}
                for j in self.jobs.values() if j["status"] == "running" for p in j["parts"]
                if p.get("worker") == worker and p["status"] in ("starting", "running")]

    def close(self) -> None:
        with self._lock:
            for j in self.jobs.values():
                self._write(j)


def decode_files(files: dict, into: Path) -> None:
    """Write a job's files into its folder on the worker."""
    for name, data in files.items():
        if "/" in name or name.startswith("."):
            raise LabratError("bad_request", f"refusing file name {name!r}")
        p = into / name
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(base64.b64decode(data))
