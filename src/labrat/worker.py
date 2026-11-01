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
import socket
import subprocess
import sys
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
        "memory_gb", "backend", "slots", "link", "link_port", "python", "pass_env", "allow_http", "hash", "stores",
        "slurm", "batch"}
REPORT_S = 10                    # the computer's state is read at most this often


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
        }
        self._report = {k: v for k, v in self._report.items() if v is not None}
        self._report_t = time.monotonic()
        return self._report

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
        while True:
            try:
                self.step()
            except LabratError as e:
                if e.code == "unauthorised":
                    raise
                log.warning("%s; trying again in 5 s", e.message)
                time.sleep(5)

    def step(self, wait: float = 20.0) -> dict:
        """Ask for work once, waiting up to wait seconds for some."""
        return self.post("next", {"report": self.report(), "wait": wait}, timeout=wait + 15)


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
