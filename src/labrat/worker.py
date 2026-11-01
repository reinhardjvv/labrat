"""A worker: a computer that runs jobs for a nest, such as a GPU workstation, a laptop or a cluster's login node.

    labrat worker init              # writes worker.toml
    labrat worker join              # joins the nest named in it, with your own token (LABRAT_TOKEN)
    labrat worker                   # runs it: asks the nest for work and runs what it is given

The worker only ever connects out to its nest, so nothing on the computer listens for connections. Every request
is signed with the key the worker made when it first started, and every job it runs is signed by the nest
(signing.py). Its owner decides in worker.toml what it takes: whose jobs, when, how much of the computer, which
code, and where its stores are. The nest cannot change any of that.

    nest = "http://nest.local:8780"
    shared = false                  # true offers it to other people, once an admin agrees
    people = ["*"]                  # whose jobs it takes when shared (its owner's always)
    accept = "any"                  # "scripts": only scripts already in the scripts folder
    when = "always"                 # "idle": only while nobody is logged in
    hours = "18:00-07:00"           # only in these hours
    cpus = 8                        # at most this much of the computer
    gpus = 1
"""
from __future__ import annotations

import ipaddress
import json
import logging
import os
import platform
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import __version__, signing
from . import config as conf
from .message import LabratError

log = logging.getLogger("labrat")
KEYS = {"nest", "name", "state_dir", "shared", "people", "accept", "scripts", "when", "hours", "cpus", "gpus",
        "memory_gb", "backend", "slots", "link", "link_port", "python", "pass_env", "allow_http", "hash", "stores", "share_hardware",
        "slurm", "batch"}
REPORT_S = 10                    # the computer's state is read at most this often
GRACE_S = 10                     # after Ctrl-C, a job has this long before SIGTERM, and as long again before SIGKILL


def load_config(path: Path) -> dict:
    """worker.toml with its defaults filled in. Unknown settings are an error, so typing mistakes show up."""
    try:
        cfg = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as e:
        raise LabratError("bad_request", f"{path}: {e}") from None
    return check_config(cfg, str(path))


def check_config(cfg: dict, where: str = "worker.toml") -> dict:
    unknown = set(cfg) - KEYS
    if unknown:
        raise LabratError("bad_request", f"{where}: unknown setting(s) {', '.join(sorted(unknown))}")
    cfg.setdefault("name", socket.gethostname().split(".")[0])
    conf.check_name(cfg["name"], "worker")
    cfg["state_dir"] = Path(cfg.get("state_dir") or conf.default_state_dir()).expanduser()
    cfg.setdefault("shared", False)
    cfg.setdefault("people", ["*"])
    cfg.setdefault("accept", "any")
    cfg.setdefault("when", "always")
    cfg.setdefault("hours", "")
    cfg.setdefault("backend", "local")
    cfg.setdefault("slots", 1 if cfg["backend"] == "local" else 32)
    cfg.setdefault("link", "")
    cfg.setdefault("link_port", 29500)
    cfg.setdefault("python", sys.executable)
    cfg.setdefault("pass_env", [])
    cfg.setdefault("allow_http", False)
    cfg.setdefault("hash", False)
    cfg.setdefault("share_hardware", False)
    cfg.setdefault("stores", {})
    if cfg["accept"] not in ("any", "scripts"):
        raise LabratError("bad_request", f"{where}: accept is \"any\" or \"scripts\"")
    if cfg["accept"] == "scripts" and not cfg.get("scripts"):
        raise LabratError("bad_request", f"{where}: accept = \"scripts\" needs scripts, the folder they are in")
    if cfg["when"] not in ("always", "idle"):
        raise LabratError("bad_request", f"{where}: when is \"always\" or \"idle\"")
    if cfg["hours"]:
        hours(cfg["hours"])
    if cfg["backend"] not in ("local", "slurm", "batch"):
        raise LabratError("bad_request", f"{where}: backend is \"local\", \"slurm\" or \"batch\"")
    if not isinstance(cfg["people"], list) or not all(isinstance(p, str) for p in cfg["people"]):
        raise LabratError("bad_request", f"{where}: people is a list of names or patterns")
    if not isinstance(cfg["pass_env"], list):
        raise LabratError("bad_request", f"{where}: pass_env is a list of environment variable names")
    for k in ("cpus", "gpus", "slots"):
        if k in cfg and (not isinstance(cfg[k], int) or cfg[k] < 0):
            raise LabratError("bad_request", f"{where}: {k} is a whole number")
    if "memory_gb" in cfg and (not isinstance(cfg["memory_gb"], (int, float)) or cfg["memory_gb"] <= 0):
        raise LabratError("bad_request", f"{where}: memory_gb is a positive number")
    if cfg["backend"] != "local":
        from .batch import check_batch
        check_batch(cfg, where)
    for name, s in cfg["stores"].items():
        conf.check_name(name, "store")
        if not isinstance(s, dict) or not s.get("path"):
            raise LabratError("bad_request", f"{where}: stores.{name} needs a path")
        if set(s) - {"path", "mount", "copy"}:
            raise LabratError("bad_request", f"{where}: stores.{name} takes path, mount and copy")
    return cfg


def hours(spec: str) -> tuple[int, int]:
    """'18:00-07:00' as minutes after midnight (start, end). The end may be the next morning."""
    try:
        a, b = spec.split("-")
        start, end = (int(x.split(":")[0]) * 60 + int(x.split(":")[1]) for x in (a.strip(), b.strip()))
    except (ValueError, IndexError):
        raise LabratError("bad_request", f"hours {spec!r}: write them as \"18:00-07:00\"") from None
    if not (0 <= start < 1440 and 0 <= end <= 1440):
        raise LabratError("bad_request", f"hours {spec!r}: times are between 00:00 and 24:00")
    return start, end


def in_hours(spec: str, t: float | None = None) -> bool:
    if not spec:
        return True
    start, end = hours(spec)
    lt = time.localtime(t)
    now = lt.tm_hour * 60 + lt.tm_min
    return start <= now < end if start <= end else now >= start or now < end


def check_url(url: str, allow_http: bool = False) -> None:
    """A worker talks plain HTTP only to a nest on a private network: loopback, private, link-local or carrier-grade
    NAT addresses (VPNs such as Tailscale use these) and .local names. Anything else needs HTTPS or allow_http."""
    u = urllib.parse.urlsplit(url)
    if u.scheme == "https" or allow_http:
        return
    if u.scheme != "http" or not u.hostname:
        raise LabratError("bad_request", f"the nest's address is http(s)://host:port, not {url!r}")
    host = u.hostname
    if host == "localhost" or host.endswith(".local"):
        return
    try:
        addrs = {ai[4][0] for ai in socket.getaddrinfo(host, u.port or 80, proto=socket.IPPROTO_TCP)}
    except OSError as e:
        raise LabratError("unavailable", f"cannot resolve {host}: {e}") from None
    cgnat = ipaddress.IPv4Network((100 << 24 | 64 << 16, 10))          # carrier-grade NAT (RFC 6598)
    for a in addrs:
        ip = ipaddress.ip_address(a.split("%")[0])
        if not (ip.is_private or ip.is_loopback or ip.is_link_local or (ip.version == 4 and ip in cgnat)):
            raise LabratError("bad_request", f"{host} is a public address: use https, or set allow_http = true in "
                              "worker.toml if the network between them is trusted")


# -- the computer ------------------------------------------------------------------------------------------------
def _run(args: list[str], timeout: float = 5.0) -> str | None:
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=True).stdout
    except (OSError, subprocess.SubprocessError):
        return None


def memory_gb() -> float | None:
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemTotal:"):
                return round(int(line.split()[1]) / 1024 ** 2, 1)
    except OSError:
        pass
    out = _run(["sysctl", "-n", "hw.memsize"])
    return round(int(out) / 1024 ** 3, 1) if out and out.strip().isdigit() else None


def gpus() -> list[dict]:
    """NVIDIA GPUs from nvidia-smi: name, memory and whether something is running on each."""
    if shutil.which("nvidia-smi") is None:
        return []
    out = _run(["nvidia-smi", "--query-gpu=index,name,memory.total,utilization.gpu,memory.used", "--format=csv,noheader,nounits"])
    found = []
    for line in (out or "").splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) == 5:
            try:
                found.append({"index": int(parts[0]), "name": parts[1], "memory_gb": round(float(parts[2]) / 1024, 1),
                              "busy": float(parts[3]) > 10 or float(parts[4]) > 512})
            except ValueError:
                continue
    return found


def idle() -> bool:
    """True when nobody is logged in at the computer."""
    out = _run(["who"])
    return out is not None and not out.strip()


class Worker:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.name = cfg["name"]
        self.state = Path(cfg["state_dir"])
        self.key = load_or_create_key(self.state)
        self.joined = self._joined()
        self.nest = str(cfg.get("nest") or self.joined.get("nest") or "").rstrip("/")
        self._report: dict = {}
        self._report_t = 0.0
        self.parts: dict[tuple[int, int], Part] = {}
        self.wait = 20.0
        self._stop = threading.Event()

    # -- joining --------------------------------------------------------------------------------------------
    def _joined(self) -> dict:
        p = self.state / "worker.json"
        return json.loads(p.read_text()) if p.is_file() else {}

    def join(self, url: str | None, token: str | None) -> dict:
        """Join a nest with its owner's token. The key goes to the nest this once; requests are signed after it."""
        from .client import Client
        url = (url or self.nest or os.environ.get("LABRAT_URL") or "").rstrip("/")
        if not url:
            raise LabratError("bad_request", "give the nest's address: nest in worker.toml, --url or LABRAT_URL")
        check_url(url, self.cfg["allow_http"])
        r = Client(url, token).request("workers", name=self.name, args={"action": "join", "key": self.key,
                                                                         "shared": bool(self.cfg["shared"])})
        self.nest, self.joined = url, {"nest": url, "name": self.name, "owner": r["owner"], "shared": r["shared"]}
        p = self.state / "worker.json"
        tmp = p.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(self.joined, f, indent=2)
        os.replace(tmp, p)
        return r

    # -- what this computer has -----------------------------------------------------------------------------
    def report(self) -> dict:
        if time.monotonic() - self._report_t < REPORT_S and self._report:
            return self._report
        cfg = self.cfg
        found = gpus()
        is_idle = idle()
        accepting = in_hours(cfg["hours"]) and (cfg["when"] == "always" or is_idle)
        stores = []
        for name, s in cfg["stores"].items():
            path = Path(str(s["path"])).expanduser()
            ok = path.is_dir() and (not s.get("mount") or os.path.ismount(Path(str(s["mount"])).expanduser()))
            free = round(shutil.disk_usage(path).free / 1024 ** 3, 1) if ok else None
            stores.append({"name": name, "available": ok, **({"free_gb": free} if free is not None else {})})
        limits = {k: cfg[k] for k in ("cpus", "gpus", "memory_gb") if k in cfg}
        from .discovery import local_addresses
        self._report = {
            "cpus": os.cpu_count() or 1, "memory_gb": memory_gb(), "gpus": found, "gpu_busy": any(g["busy"] for g in found),
            "load": round(os.getloadavg()[0], 2) if hasattr(os, "getloadavg") else None, "idle": is_idle,
            "accepting": accepting, "backend": cfg["backend"], "slots": cfg["slots"], "accept": cfg["accept"],
            "people": cfg["people"], "when": cfg["when"], "hours": cfg["hours"], "stores": stores, "limits": limits,
            "host": socket.gethostname(), "platform": platform.platform(terse=True), "python": platform.python_version(),
            "labrat": __version__, "addresses": local_addresses(), "link": cfg["link"],
            **({"hardware": self.hardware()} if cfg["share_hardware"] else {}),
        }
        self._report = {k: v for k, v in self._report.items() if v is not None}
        self._report_t = time.monotonic()
        return self._report

    def hardware(self) -> dict:
        """What this computer is (hardware.py), read once, with the disk speed setup measured and free space now."""
        if getattr(self, "_hardware", None) is None:
            from .hardware import describe
            self._hardware = describe()
            saved = self.state / "hardware.json"
            if saved.is_file():
                try:
                    speeds = [d.get("write_mbps") for d in json.loads(saved.read_text()).get("disks", []) if d.get("system")]
                except (OSError, ValueError):
                    speeds = []
                for d in self._hardware.get("disks", []):
                    if d.get("system") and speeds and speeds[0]:
                        d["write_mbps"] = speeds[0]
        for d in self._hardware.get("disks", []):
            if d.get("system"):
                d["free_gb"] = round(shutil.disk_usage("/").free / 1e9, 1)
        return self._hardware

    # -- talking to the nest --------------------------------------------------------------------------------
    def post(self, action: str, body: dict | None = None, timeout: float = 30.0) -> dict:
        """One signed request. The reply is checked against its signature before anything in it is used."""
        if not self.nest:
            raise LabratError("bad_request", "this worker has not joined a nest yet (labrat worker join)")
        data = json.dumps({"action": action, **(body or {})}).encode()
        header, nonce = signing.request_header(self.name, self.key, data)
        req = urllib.request.Request(self.nest + "/api/v1/worker", data=data, method="POST",
                                     headers={"Content-Type": "application/json", "Authorization": header})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw, sig = r.read(), r.headers.get("X-Labrat-Signature", "")
        except urllib.error.HTTPError as e:
            raw, sig = e.read(), e.headers.get("X-Labrat-Signature", "")
        except OSError as e:
            raise LabratError("unavailable", f"cannot reach the nest at {self.nest}: {e}") from None
        try:
            reply = json.loads(raw)
        except ValueError:
            raise LabratError("unavailable", f"the nest at {self.nest} did not answer in JSON") from None
        if sig and signing.reply_signature(self.key, nonce, raw) == sig:
            if not reply.get("ok"):
                err = reply.get("error") or {}
                raise LabratError(err.get("code", "internal"), err.get("message", "no reason given"))
            return reply.get("value") or {}
        err = (reply.get("error") or {}) if isinstance(reply, dict) else {}
        if err.get("code") == "unauthorised":
            raise LabratError("unauthorised", f"the nest refused this worker: {err.get('message')}")
        raise LabratError("unauthorised", "the nest's reply is not signed with this worker's key; not trusting it")

    def hello(self) -> dict:
        return self.post("hello", {"report": self.report()})

    def run_forever(self) -> None:
        check_url(self.nest, self.cfg["allow_http"])
        me = self.hello()
        log.info("worker %s is online at %s (owner %s, %s)", self.name, self.nest, me["owner"],
                 "shared" if me["shared"] == "yes" else "offered to others, waiting for an admin" if me["shared"] == "asked" else "personal")
        while not self._stop.is_set():
            try:
                self.step()
            except LabratError as e:
                if e.code == "unauthorised":
                    raise
                log.warning("%s; trying again in 5 s", e.message)
                self._stop.wait(5)

    def step(self) -> dict:
        """Ask for work once, waiting up to self.wait seconds for some, and start or stop what the nest says."""
        running = [{"job": j, "rank": r} for (j, r), part in list(self.parts.items()) if part.alive()]
        for key in [k for k, part in self.parts.items() if not part.alive()]:
            self.parts.pop(key, None)
        reply = self.post("next", {"report": self.report(), "wait": self.wait, "running": running}, timeout=self.wait + 15)
        for payload in reply.get("parts", []):
            if self.cfg["backend"] == "local":
                part = Part(self, payload)
            else:
                from .batch import BatchPart
                part = BatchPart(self, payload)
            self.parts[(part.job, part.rank)] = part
            part.start()
        for s in reply.get("stop", []):
            part = self.parts.get((int(s["job"]), int(s["rank"])))
            if part is not None:
                part.stop("stopped from the nest")
        return reply

    def close(self) -> None:
        self._stop.set()
        for part in list(self.parts.values()):
            part.stop("the worker stopped")

    # -- the owner's rules ----------------------------------------------------------------------------------
    def refuses(self, payload: dict) -> str | None:
        """Why this worker will not run a part, or None. These are the owner's rules, checked here whatever the
        nest says."""
        from fnmatch import fnmatch
        cfg, person = self.cfg, str(payload.get("person", ""))
        owner = self.joined.get("owner")
        if person != owner and not (cfg["shared"] and any(fnmatch(person, p) for p in cfg["people"])):
            return f"{self.name} does not take {person}'s jobs"
        if payload.get("files") and cfg["accept"] != "any":
            return f"{self.name} runs only scripts already in its scripts folder"
        if not in_hours(cfg["hours"]) or (cfg["when"] == "idle" and not idle()):
            return f"{self.name} is not taking work now"
        if payload.get("exclusive") and any(g["busy"] for g in gpus()):
            return f"{self.name}'s GPUs are in use"
        return None


class Part:
    """One part of a job, run on this computer as a process of its own with a clean environment."""

    def __init__(self, worker: Worker, payload: dict):
        self.w, self.p = worker, payload
        self.job, self.rank = int(payload["job"]), int(payload["rank"])
        self.dir = Path(worker.cfg["state_dir"]) / "jobs" / f"{self.job}-{self.rank}"
        self.proc: subprocess.Popen | None = None
        self._why: str | None = None
        self._stopped_at: float | None = None
        self._thread = threading.Thread(target=self.run, name=f"labrat-job-{self.job}-{self.rank}", daemon=True)
        self._lines: list[str] = []
        self._lines_lock = threading.Lock()
        self.folders: dict[str, Path] = {}
        self._out = None

    def start(self) -> None:
        self._thread.start()

    def alive(self) -> bool:
        return self._thread.is_alive() or not self._thread.ident

    def stop(self, why: str) -> None:
        if self._why is None:
            self._why = why

    def _send(self, action: str, **body) -> dict:
        return self.w.post(action, {"job": self.job, "rank": self.rank, **body})

    # -- getting it ready -----------------------------------------------------------------------------------
    def command(self) -> list[str]:
        from .jobs import decode_files
        p, cfg = self.p, self.w.cfg
        script = str(p["script"])
        if p.get("files"):
            decode_files(p["files"], self.dir)
            path = self.dir / script
        else:
            folder = Path(str(cfg.get("scripts") or "")).expanduser().resolve()
            path = (folder / script).resolve()
            if not cfg.get("scripts") or not path.is_relative_to(folder) or not path.is_file():
                raise LabratError("not_found", f"{script} is not in {self.w.name}'s scripts folder")
        if path.suffix == ".py":
            return [cfg["python"], str(path), *p.get("args", [])]
        if path.suffix == ".sh":
            return ["/bin/sh", str(path), *p.get("args", [])]
        if not p.get("files") and os.access(path, os.X_OK):
            return [str(path), *p.get("args", [])]
        raise LabratError("bad_request", f"{script}: a job runs a .py or .sh script, or a program in the scripts folder")

    def environment(self) -> dict:
        """A clean environment: what a program needs to run, what the owner passes on, the job's own variables,
        and LABRAT's. Nothing else from the worker's own environment reaches the job."""
        from .jobs import part_token
        p, cfg = self.p, self.w.cfg
        env = {k: os.environ[k] for k in ("PATH", "LANG", "LC_ALL", "LC_CTYPE", "TZ") if k in os.environ}
        env.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
        user = os.environ.get("USER") or os.environ.get("LOGNAME") or ""
        env.update(HOME=os.environ.get("HOME") or str(Path.home()), USER=user, LOGNAME=user, SHELL="/bin/sh",
                   TMPDIR=str(self.dir / "tmp"), PYTHONUNBUFFERED="1")
        env.update({k: os.environ[k] for k in cfg["pass_env"] if k in os.environ})
        env.update({str(k): str(v) for k, v in (p.get("env") or {}).items()})
        n = int(p["world_size"])
        env.update(LABRAT_URL=self.w.nest, LABRAT_TOKEN=part_token(self.w.key, self.job, self.rank), LABRAT_JOB=str(self.job),
                   LABRAT_JOB_NAME=str(p["name"]), LABRAT_RANK=str(self.rank), LABRAT_WORLD_SIZE=str(n),
                   LABRAT_LEADER=str(p["leader"]), LABRAT_WORKER=self.w.name,
                   MASTER_ADDR=str(p["leader"]), MASTER_PORT=str(p["port"]), RANK=str(self.rank), WORLD_SIZE=str(n), LOCAL_RANK="0")
        devices = self.gpus()
        if devices is not None:
            env["CUDA_VISIBLE_DEVICES"] = ",".join(map(str, devices))
        for purpose, folder in self.folders.items():
            env[f"LABRAT_STORE_{purpose.upper()}"] = str(folder)
        return env

    # -- where results go -----------------------------------------------------------------------------------
    def _root(self, store: str) -> Path:
        s = self.w.cfg["stores"].get(store)
        if s is None:
            raise LabratError("not_found", f"{self.w.name} has no store {store!r} in its worker.toml")
        if s.get("mount") and not os.path.ismount(Path(str(s["mount"])).expanduser()):
            raise LabratError("unavailable", f"store {store}: {s['mount']} is not mounted")
        root = Path(str(s["path"])).expanduser().resolve()
        if not root.is_dir():
            raise LabratError("unavailable", f"store {store}: {root} is not there")
        return root

    def _inside(self, root: Path, rel: str) -> Path:
        target = (root / rel).resolve()
        if not target.is_relative_to(root):
            raise LabratError("forbidden", f"{rel} leads outside the store")
        return target

    def _expand(self, pattern: str) -> str:
        from .jobs import expand
        return expand(pattern, self.job, str(self.p["name"]), str(self.p["person"]), self.w.name, self.rank)

    def stores(self) -> None:
        """Make this part's folders on the stores, as this computer mounts them. A folder another job left files in
        is never written into: the job's number in the path (%j, the default) keeps every job's results apart."""
        for purpose, w in (self.p.get("stores") or {}).items():
            root = self._root(w["store"])
            if w.get("fixed"):                                      # an open experiment's own folder
                folder = self._inside(root, w["path"])
                folder.mkdir(parents=True, exist_ok=True)
            else:
                folder = self._inside(root, self._expand(w["path"]))
                folder.mkdir(parents=True, exist_ok=True)
                marker = folder / ".labrat-job"
                mine = folder / f".labrat-job.{self.w.name}.{self.rank}"
                try:
                    if any(not x.name.startswith(".labrat-job") for x in folder.iterdir()) and not marker.exists():
                        raise FileExistsError
                    mine.write_text(f"{self.job}\n")
                    os.link(mine, marker)                          # appears whole, or not at all if another part was first
                except FileExistsError:
                    if not marker.is_file() or marker.read_text().strip() != str(self.job):
                        raise LabratError("bad_request", f"{w['store']}:{folder.relative_to(root)} already holds another job's "
                                          "files; put %j in the path") from None
                finally:
                    mine.unlink(missing_ok=True)
            self.folders[purpose] = folder
        out = self.p.get("output")
        if out:
            root = self._root(out["store"])
            rel = self._expand(out["path"])
            if int(self.p["world_size"]) > 1 and "%t" not in out["path"]:
                rel += f".{self.rank}"
            path = self._inside(root, rel)
        elif "data" in self.folders:
            path = self.folders["data"] / ("output.txt" if int(self.p["world_size"]) == 1 else f"output-{self.rank}.txt")
        else:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._out = os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644), "w", encoding="utf-8")
        except FileExistsError:
            raise LabratError("bad_request", f"{path.name} exists already; LABRAT never writes over a file") from None

    def manifest(self) -> dict:
        """Every file in this part's folders: its path, size and, with hash = true, its SHA-256."""
        import hashlib
        out = {}
        for purpose, folder in self.folders.items():
            w = self.p["stores"][purpose]
            files, more = [], False
            for f in sorted(folder.rglob("*")):
                if not f.is_file() or f.name.startswith(".labrat-job"):
                    continue
                if len(files) >= 10000:
                    more = True
                    break
                e = {"path": str(f.relative_to(folder)), "size": f.stat().st_size}
                if self.w.cfg["hash"]:
                    h = hashlib.sha256()
                    with open(f, "rb") as fh:
                        while chunk := fh.read(1 << 20):
                            h.update(chunk)
                    e["sha256"] = h.hexdigest()
                files.append(e)
            out[purpose] = {"store": w["store"], "folder": str(folder.relative_to(self._root(w["store"]))), "files": files,
                            **({"more": True} if more else {})}
        return out

    def finish_stores(self, code: int | None, error: str | None) -> tuple[dict, str | None]:
        """The manifest, the job's record beside its results, and each store's copy command. Returns the manifest
        and what went wrong, if anything."""
        import shlex
        problems = []
        if self._out is not None:
            self._out.close()
            self._out = None
        try:
            manifest = self.manifest()
        except (OSError, LabratError) as e:
            return {}, f"listing the results: {getattr(e, 'message', e)}"
        if self.rank == 0 and "data" in self.folders:
            record = {"job": self.job, "name": self.p["name"], "person": self.p["person"], "by": self.p["by"],
                      "worker": self.w.name, "script": self.p["script"], "args": self.p.get("args", []),
                      "needs": self.p.get("needs", {}), "exit_code": code, **({"error": error} if error else {}),
                      "ended": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "files": manifest}
            try:
                (self.folders["data"] / f"job-{self.job}.json").write_text(json.dumps(record, indent=2) + "\n")
            except OSError as e:
                problems.append(f"writing job-{self.job}.json: {e}")
        for purpose, folder in self.folders.items():
            w = self.p["stores"][purpose]
            copy = self.w.cfg["stores"][w["store"]].get("copy")
            if not copy:
                continue
            from .batch import fill
            rel = str(folder.relative_to(self._root(w["store"])))
            args = [fill(a, {"src": str(folder), "rel": rel, "store": w["store"]}) for a in shlex.split(copy)]
            try:
                r = subprocess.run(args, capture_output=True, text=True, timeout=24 * 3600)
                if r.returncode:
                    problems.append(f"copying {purpose} failed: {(r.stderr or r.stdout).strip()[:300]}")
            except (OSError, subprocess.SubprocessError) as e:
                problems.append(f"copying {purpose} failed: {e}")
        return manifest, "; ".join(problems) or None

    def gpus(self) -> list[int] | None:
        """The GPUs this part may use: as many free ones as it asked for, within the owner's limit."""
        want, limit = (self.p.get("needs") or {}).get("gpus"), self.w.cfg.get("gpus")
        if want is None and limit is None:
            return None
        found = gpus()
        free = [g["index"] for g in found if not g["busy"]] or [g["index"] for g in found]
        n = int(want) if want is not None else len(found)
        return free[:min(n, limit) if limit is not None else n]

    def cpus(self) -> None:
        want = (self.p.get("needs") or {}).get("cpus") or self.w.cfg.get("cpus")
        if want and hasattr(os, "sched_setaffinity") and self.proc is not None:
            try:
                os.sched_setaffinity(self.proc.pid, sorted(os.sched_getaffinity(0))[:int(want)])
            except OSError as e:
                log.warning("job %d: cannot limit it to %s CPUs: %s", self.job, want, e)

    # -- running it -----------------------------------------------------------------------------------------
    def run(self) -> None:
        code, error = None, None
        try:
            why = self.w.refuses(self.p)
            if why:
                raise LabratError("forbidden", why)
            if self.dir.exists():
                shutil.rmtree(self.dir)
            (self.dir / "tmp").mkdir(parents=True, mode=0o700)
            self.stores()
            code, error = self.execute()
        except LabratError as e:
            error = e.message
        except OSError as e:
            error = f"could not start it: {e}"
        finally:
            self._flush()
            manifest = None
            if self.folders or self._out is not None:
                manifest, problem = self.finish_stores(code, error)
                if problem:
                    error = f"{error}; {problem}" if error else problem
            for _ in range(5):
                try:
                    self._send("done", code=code, **({"error": error} if error else {}), **({"manifest": manifest} if manifest else {}))
                    break
                except LabratError:
                    time.sleep(2)
            self.cleanup()
            log.info("job %d rank %d ended: %s", self.job, self.rank, error or f"exit code {code}")

    def cleanup(self) -> None:
        shutil.rmtree(self.dir, ignore_errors=True)

    def execute(self) -> tuple[int | None, str | None]:
        """Run it here, as a process of its own."""
        cmd = self.command()
        self.proc = subprocess.Popen(cmd, cwd=self.dir, env=self.environment(), stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True)
        self.cpus()
        log.info("job %d rank %d (%s for %s) started", self.job, self.rank, self.p["name"], self.p["person"])
        if self._send("started").get("stop"):
            self.stop("stopped from the nest")
        return self._watch()

    def _read(self) -> None:
        for raw in self.proc.stdout:
            with self._lines_lock:
                self._lines.append(raw.decode(errors="replace").rstrip("\n"))

    def _flush(self) -> bool:
        """Send what the job printed since last time. True when the nest asks for it to stop."""
        with self._lines_lock:
            text, self._lines = "\n".join(self._lines), []
        if not text:
            return False
        if self._out is not None:
            try:
                self._out.write(text + "\n")
                self._out.flush()
            except OSError:
                pass
        try:
            return bool(self._send("output", text=text).get("stop"))
        except LabratError:
            with self._lines_lock:
                self._lines.insert(0, text)
            return False

    def _watch(self) -> tuple[int, str | None]:
        reader = threading.Thread(target=self._read, daemon=True)
        reader.start()
        limit = self.p.get("minutes")
        started = time.monotonic()
        while self.proc.poll() is None or reader.is_alive():
            reader.join(0.5)
            if self._flush():
                self.stop("stopped from the nest")
            if limit and time.monotonic() - started > float(limit) * 60:
                self.stop(f"the time limit of {float(limit):g} min was reached")
            if self._why is not None and self.proc.poll() is None:
                self._signal()
        code = self.proc.wait()
        error = self._why if self._why and self._why != "stopped from the nest" else None
        return code, error

    def _signal(self) -> None:
        """Ctrl-C first, so the job can clean up and leave its devices safe; then SIGTERM; then SIGKILL."""
        now = time.monotonic()
        if self._stopped_at is None:
            self._stopped_at = now
            sig = signal.SIGINT
        elif now - self._stopped_at > 2 * GRACE_S:
            sig = signal.SIGKILL
        elif now - self._stopped_at > GRACE_S:
            sig = signal.SIGTERM
        else:
            return
        try:
            os.killpg(self.proc.pid, sig)
        except OSError:
            pass


def load_or_create_key(state_dir: Path) -> str:
    p = Path(state_dir) / "worker.key"
    if p.is_file():
        return p.read_text().strip()
    p.parent.mkdir(parents=True, exist_ok=True)
    key = secrets.token_urlsafe(32)
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(key + "\n")
    log.info("made a new worker key in %s", p)
    return key


def config_text(name: str, nest: str, state_line: str = "") -> str:
    return f'''# LABRAT worker configuration. Reference: docs/workers.md
name = "{name}"
nest = "{nest}"
{state_line}
# shared = true            # offer this computer to other people as well, once an admin agrees
# people = ["*"]           # whose jobs it takes when shared (its owner's always)
# accept = "scripts"       # only run scripts already in the scripts folder
# scripts = "/srv/labrat-jobs"
# when = "idle"            # only while nobody is logged in
# hours = "18:00-07:00"    # only in these hours
# cpus = 8                 # at most this much of the computer
# gpus = 1
# link = "192.0.2.2"       # this computer's address on a fast link, for jobs across several workers

# [stores.nas]             # where the lab's stores are on this computer
# path = "/mnt/labnas/labrat"
'''
