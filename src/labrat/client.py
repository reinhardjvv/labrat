"""A Python client for a nest or rat.

    from labrat.client import Client
    lab = Client("http://nest.local:8780", token="...")      # or set LABRAT_URL and LABRAT_TOKEN
    lab.set("bench-rat/psu", "VoltageLevel", 3.3, channel="1")
    with lab.locked("bench-rat/psu"):
        lab.set("bench-rat/psu", "OutputEnabled", True, channel="1")

Each thread keeps its connection open between requests. lab.direct("bench-rat/slm") talks to a rat's device
without the nest in between, under a lease (lease.py).
"""
from __future__ import annotations

import hmac
import http.client
import json
import logging
import os
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from typing import Any

from .message import LabratError

log = logging.getLogger("labrat")


class Client:
    def __init__(self, url: str | None = None, token: str | None = None, timeout: float = 30.0, scheme: str = "Bearer"):
        self.url = (url or os.environ.get("LABRAT_URL") or "http://127.0.0.1:8780").rstrip("/")
        self.token = token if token is not None else os.environ.get("LABRAT_TOKEN", "")
        self.timeout = timeout
        self.scheme = scheme                         # "Lease" when talking to a rat directly
        self._local = threading.local()

    def _conn(self, fresh: bool = False) -> http.client.HTTPConnection:
        c = getattr(self._local, "conn", None)
        if c is None or fresh:
            if c is not None:
                c.close()
            u = urllib.parse.urlsplit(self.url)
            cls = http.client.HTTPSConnection if u.scheme == "https" else http.client.HTTPConnection
            c = cls(u.hostname, u.port, timeout=self.timeout)
            self._local.conn = c
        return c

    def _post(self, body: bytes, headers: dict) -> tuple[int, bytes]:
        """POST to the message endpoint on this thread's open connection, opening a new one if the server closed it."""
        path = urllib.parse.urlsplit(self.url).path.rstrip("/") + "/api/v1/message"
        headers = {**headers, "Authorization": f"{self.scheme} {self.token}"}
        for attempt in (0, 1):
            reused = getattr(self._local, "conn", None) is not None and attempt == 0
            c = self._conn(fresh=attempt == 1)
            try:
                c.request("POST", path, body=body, headers=headers)
                r = c.getresponse()
                data = r.read()
            except (http.client.RemoteDisconnected, ConnectionResetError, BrokenPipeError) as e:
                c.close()
                self._local.conn = None
                if reused:
                    continue                         # an idle connection the server had closed: once more, fresh
                raise LabratError("unavailable", f"cannot reach {self.url}: {e}") from None
            except (OSError, http.client.HTTPException) as e:
                c.close()
                self._local.conn = None
                raise LabratError("unavailable", f"cannot reach {self.url}: {e}") from None
            if (r.getheader("Connection") or "").lower() == "close":
                c.close()
                self._local.conn = None
            return r.status, data
        raise LabratError("unavailable", f"cannot reach {self.url}")

    def send(self, msg: dict | list) -> Any:
        """Send one message (or a list) and return the reply as it came. A message whose args hold bytes, such as a
        frame for a display, travels with those bytes as the raw body."""
        args = msg.get("args") if isinstance(msg, dict) else None
        raw = [k for k, v in (args or {}).items() if isinstance(v, (bytes, bytearray))]
        if raw:
            if len(raw) > 1:
                raise LabratError("bad_request", "only one argument can travel as raw bytes")
            head = {**msg, "args": {k: v for k, v in args.items() if k != raw[0]}}
            status, data = self._post(bytes(args[raw[0]]), {"Content-Type": "application/octet-stream",
                                                            "X-Labrat-Message": json.dumps(head), "X-Labrat-Data": raw[0]})
        else:
            status, data = self._post(json.dumps(msg).encode(), {"Content-Type": "application/json"})
        try:
            return json.loads(data)
        except ValueError:
            raise LabratError("unavailable", f"{self.url} answered HTTP {status}") from None

    def request(self, op: str, **fields) -> Any:
        """Send one message and return its value, or raise its error as a LabratError."""
        reply = self.send({"op": op, **{k: v for k, v in fields.items() if v is not None}})
        if not reply.get("ok"):
            err = reply.get("error") or {}
            extra = {k: v for k, v in err.items() if k not in ("code", "message")}
            raise LabratError(err.get("code", "internal"), err.get("message", "no reason given"), **extra)
        return reply.get("value")

    def health(self) -> dict:
        with urllib.request.urlopen(self.url + "/api/v1/health", timeout=self.timeout) as r:
            return json.loads(r.read())["value"]

    def devices(self, select: str | None = None, sort: str | None = None) -> list[dict]:
        """The devices, optionally filtered by a selector ('oscilloscope tektronix/*') and sorted by a field."""
        args = {k: v for k, v in (("select", select), ("sort", sort)) if v}
        return self.request("list", args=args or None)

    def find(self, select: str) -> list[str]:
        """The ids of the devices a selector matches."""
        return [d["device"] for d in self.devices(select)]

    def device(self, select: str) -> Device:
        """A handle on the one device an id or a selector names, like loading a model by name:

            scope = lab.device("oscilloscope tektronix/mso54")
            scope.set("HorizontalScale", 1e-6)
        """
        ids = [d["device"] for d in self.devices()]
        if select in ids:
            return Device(self, select)
        found = self.find(select)
        if len(found) != 1:
            raise LabratError("not_found" if not found else "bad_request",
                              f"{select!r} matches {', '.join(found) if found else 'no device'}; name exactly one")
        return Device(self, found[0])

    def describe(self, device: str) -> dict:
        return self.request("describe", device=device)

    def get(self, device: str, name: str, channel: str | None = None) -> Any:
        return self.request("get", device=device, name=name, channel=channel)

    def set(self, device: str, name: str, value: Any, channel: str | None = None) -> Any:
        return self.request("set", device=device, name=name, value=value, channel=channel)

    def call(self, device: str, name: str, channel: str | None = None, **args) -> Any:
        return self.request("call", device=device, name=name, channel=channel, args=args or None)

    def lock(self, device: str, minutes: float | None = None) -> dict:
        return self.request("lock", device=device, args={"minutes": minutes} if minutes else None)

    def unlock(self, device: str) -> dict:
        return self.request("unlock", device=device)

    @contextmanager
    def locked(self, device: str, minutes: float | None = None):
        self.lock(device, minutes)
        try:
            yield self
        finally:
            self.unlock(device)

    def stop(self, reason: str = "") -> dict:
        return self.request("stop", args={"reason": reason} if reason else None)

    def approvals(self) -> list[dict]:
        return self.request("approvals")

    def approval(self, approval_id: str) -> dict:
        return self.request("approval", name=approval_id)

    def approve(self, approval_id: str) -> dict:
        return self.request("approve", name=approval_id)

    def deny(self, approval_id: str) -> dict:
        return self.request("deny", name=approval_id)

    def recordings(self) -> dict:
        return self.request("record")

    def record(self, name: str, action: str = "start", **spec) -> dict:
        """Start, stop or ask about a recording. A start may give a whole recording: device, read or call, every_s..."""
        return self.request("record", name=name, args={"action": action, **spec})

    # -- experiments and their files ------------------------------------------------------------------------
    def experiments(self, person: str | None = None) -> list[dict]:
        """Your experiments, newest first (admins may name a person, or '*' for everyone's)."""
        return self.request("experiment", args={"action": "list", **({"person": person} if person else {})})

    def experiment(self, name: str | None = None) -> dict:
        """The experiment you have open, or one of yours by name."""
        return self.request("experiment", name=name, args={"action": "status"})

    def start_experiment(self, name: str, description: str = "") -> dict:
        return self.request("experiment", name=name, args={"action": "start", "description": description})

    def stop_experiment(self) -> dict:
        return self.request("experiment", args={"action": "stop"})

    def note(self, text: str) -> dict:
        """A note in the experiment you have open."""
        return self.request("experiment", args={"action": "note", "text": text})

    @contextmanager
    def experimenting(self, name: str, description: str = ""):
        """An experiment open for the block, stopped at its end even when the block fails:

            with lab.experimenting("supply-sweep", "supply from 1 to 5 V"):
                lab.record("scope-traces")
                ...
        """
        exp = self.start_experiment(name, description)
        try:
            yield exp
        finally:
            self.stop_experiment()

    def files(self, path: str = "", person: str | None = None) -> list[dict]:
        """The files in a folder of your experiments ('' for all of them)."""
        return self.request("files", name=path or None, args={"person": person} if person else None)["files"]

    def read_file(self, path: str, offset: int = 0, length: int | None = None, person: str | None = None) -> dict:
        """Part of a file: {text or base64, size, offset, length, more}."""
        args = {"offset": offset, **({"length": length} if length else {}), **({"person": person} if person else {})}
        return self.request("files", name=path, args=args)

    def download(self, path: str, to: str | os.PathLike | None = None, person: str | None = None) -> str:
        """A whole file from your experiments, saved to 'to' (by default its own name here). Returns where."""
        from urllib.parse import quote
        q = f"?person={quote(person)}" if person else ""
        req = urllib.request.Request(f"{self.url}/api/v1/files/{quote(path)}{q}", headers={"Authorization": f"Bearer {self.token}"})
        dest = os.fspath(to) if to else os.path.basename(path)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r, open(dest, "wb") as f:
                while chunk := r.read(1 << 20):
                    f.write(chunk)
        except urllib.error.HTTPError as e:
            try:
                err = json.loads(e.read()).get("error") or {}
            except ValueError:
                err = {}
            raise LabratError(err.get("code", "internal"), err.get("message", f"HTTP {e.code}")) from None
        except OSError as e:
            raise LabratError("unavailable", f"cannot download {path}: {e}") from None
        return dest

    # -- people ---------------------------------------------------------------------------------------------
    def who(self) -> dict:
        """Who holds each device, who changed it last and when, and when each person was last active."""
        return self.request("who")

    def users(self) -> list[dict]:
        return self.request("users")

    def add_user(self, name: str, role: str = "operator", for_: str | None = None) -> dict:
        """A new user (admins only). The reply holds their token, shown this once."""
        return self.request("users", name=name, args={"action": "add", "role": role, **({"for": for_} if for_ else {})})

    def remove_user(self, name: str) -> dict:
        return self.request("users", name=name, args={"action": "remove"})

    # -- leases: talking to a rat's devices directly ------------------------------------------------------
    def lease(self, *devices: str, minutes: float | None = None) -> dict:
        """A lease on devices of one rat: the token, the secret that proves the rat is real, and its addresses."""
        return self.request("lease", args={"action": "grant", "devices": list(devices), **({"minutes": minutes} if minutes else {})})

    def renew_lease(self, lease_id: str) -> dict:
        return self.request("lease", name=lease_id, args={"action": "renew"})

    def release_lease(self, lease_id: str) -> dict:
        return self.request("lease", name=lease_id, args={"action": "release"})

    def direct(self, device: str, minutes: float = 1.0) -> DirectDevice:
        """A rat's device without the nest in between, on the fastest path that answers, renewed in the background:

            with lab.direct("optics-rat/slm") as slm:
                for frame in frames:
                    slm.call("ShowImage", data=frame)            # bytes go as the raw body
        """
        return DirectDevice(self, device, minutes)

    # -- computers joining ----------------------------------------------------------------------------------
    def joins(self) -> list[dict]:
        """Computers waiting for someone to approve their code."""
        return self.request("join", args={"action": "list"})

    def approve_join(self, code: str, owner: str | None = None, shared: bool | None = None) -> dict:
        """Let a waiting computer in: as your own personal worker, or (admins) someone else's or shared."""
        return self.request("join", name=code, args={"action": "approve", **({"owner": owner} if owner else {}),
                                                     **({"shared": shared} if shared is not None else {})})

    def deny_join(self, code: str) -> dict:
        return self.request("join", name=code, args={"action": "deny"})

    def new_join_key(self, uses: int = 1, hours: float = 24, shared: bool = False, owner: str | None = None,
                     roles: list | None = None) -> dict:
        """A join key (admins): computers that run labrat setup --key with it join at once. Shown this once."""
        return self.request("join", args={"action": "key_new", "uses": uses, "hours": hours, "shared": shared,
                                          **({"owner": owner} if owner else {}), **({"roles": roles} if roles else {})})

    def join_keys(self) -> list[dict]:
        return self.request("join", args={"action": "keys"})

    def revoke_join_key(self, key_id: str) -> dict:
        return self.request("join", name=key_id, args={"action": "key_revoke"})

    # -- workers and jobs -----------------------------------------------------------------------------------
    def workers(self) -> list[dict]:
        """The computers that run jobs, with their owner, sharing and what each has."""
        return self.request("workers", args={"action": "list"})

    def run_job(self, script: str | os.PathLike, files: list | tuple = (), args: list | tuple = (), *, installed: bool = False,
                on: list | None = None, nodes: int | None = None, cpus: int | None = None, memory_gb: float | None = None,
                gpus: int | None = None, gpu_memory_gb: float | None = None, exclusive: bool = False, devices: list | tuple = (),
                env: dict | None = None, minutes: float | None = None, name: str | None = None, output: str | None = None,
                stores: dict | None = None) -> dict:
        """Start a job: a script on this computer, sent with the files named, or with installed=True the name of a script
        already in the worker's scripts folder. It waits in the queue until workers that fit are free:

            job = lab.run_job("fit.py", ["model.py"], ["--steps", "40"], gpus=1, stores={"raw": "nas-raw"})
            lab.job_output(job["id"])

        stores maps a purpose to 'store' or 'store:path', and output is 'store:path' for what the job prints, with
        Slurm's codes in paths: %j job, %x name, %u person, %N worker, %t rank.
        """
        import base64
        from pathlib import Path
        if installed:
            sent, script_name = {}, str(script)
        else:
            paths = [Path(script), *map(Path, files)]
            sent = {q.name: base64.b64encode(q.read_bytes()).decode() for q in paths}
            script_name = paths[0].name
        needs = {k: v for k, v in (("nodes", nodes), ("cpus", cpus), ("memory_gb", memory_gb), ("gpus", gpus),
                                   ("gpu_memory_gb", gpu_memory_gb)) if v is not None}
        spec = {"script": script_name, "files": sent, "args": [str(a) for a in args], "on": list(on or []), "needs": needs,
                "exclusive": exclusive, "devices": list(devices), "env": dict(env or {}),
                **{k: v for k, v in (("minutes", minutes), ("name", name), ("output", output), ("stores", stores)) if v is not None}}
        return self.request("job", args={"action": "submit", "spec": spec})

    def jobs(self, person: str | None = None) -> list[dict]:
        """Your jobs, newest first (admins may name a person, or '*' for everyone's)."""
        return self.request("job", args={"action": "list", **({"person": person} if person else {})})

    def job(self, job_id: int) -> dict:
        return self.request("job", name=str(job_id), args={"action": "status"})

    def job_output(self, job_id: int, rank: int = 0, since: int = 0) -> dict:
        """What a part of a job printed, from line 'since' on: {text, next, status}."""
        return self.request("job", name=str(job_id), args={"action": "output", "rank": rank, "since": since})

    def stop_job(self, job_id: int) -> dict:
        return self.request("job", name=str(job_id), args={"action": "stop"})

    # -- the workbench: from an unknown device to a profile and a device ----------------------------------
    def _on(self, rat: str | None, **args) -> dict:
        return {k: v for k, v in {**args, "rat": rat}.items() if v is not None}

    def scan(self, rat: str | None = None, probe: bool = False) -> list[dict]:
        """What is plugged in on the nest (or a rat), with the catalog profiles that fit."""
        return self.request("scan", args=self._on(rat, probe=probe or None))

    def probe(self, target: str, rat: str | None = None, protocol: str | None = None, sends: list[str] | None = None,
              baud: int | None = None, line_ending: str | None = None) -> dict:
        """Safe questions to a device, and its answers: a transcript."""
        return self.request("probe", args=self._on(rat, target=target, protocol=protocol, sends=sends, baud=baud, line_ending=line_ending))

    def capture(self, action: str = "status", kind: str | None = None, target: str | None = None, rat: str | None = None,
                baud: int | None = None):
        return self.request("capture", args=self._on(rat, action=action, kind=kind, target=target, baud=baud))

    def profiles(self, select: str | None = None, rat: str | None = None) -> list[dict]:
        return self.request("profile", args=self._on(rat, action="list", select=select))

    def profile(self, profile_id: str, rat: str | None = None) -> dict:
        return self.request("profile", name=profile_id, args=self._on(rat, action="show"))

    def try_profile(self, profile: str, rat: str | None = None, **device) -> dict:
        """Read every attribute a profile describes from the real device, changing nothing. profile is an id from
        the catalog or a whole profile as TOML text; device holds the port or address and any overrides."""
        args = {"toml": profile} if "\n" in profile else {}
        return self.request("profile", name=None if args else profile, args=self._on(rat, action="try", device=device, **args))

    def save_profile(self, toml: str, transcripts: list[str] | None = None, overwrite: bool = False, rat: str | None = None) -> dict:
        """Put a profile in the catalog, with the replies of the named probes and captures as its recordings."""
        return self.request("profile", args=self._on(rat, action="save", toml=toml, transcripts=transcripts, overwrite=overwrite or None))

    def add_device(self, name: str, rat: str | None = None, **config) -> dict:
        """A device added while the nest runs (admins only), such as add_device("ups", profile="mecer/me-2000-wpru",
        port="hid:///dev/hidraw0"). A name like bench-rat/ups adds it on that rat."""
        return self.request("device", name=name, args=self._on(rat, action="add", config=config))

    def remove_device(self, name: str) -> dict:
        return self.request("device", name=name, args={"action": "remove"})

    def rats(self) -> list[dict]:
        return self.request("rats")

    def adopt(self, name: str, key: str, url: str | None = None) -> dict:
        return self.request("adopt", name=name, value=key, args={"url": url} if url else None)

    def forget(self, name: str) -> dict:
        return self.request("forget", name=name)


class Device:
    """One device on a nest or rat, so its id is not repeated in every call."""

    def __init__(self, client: Client, device: str):
        self.client, self.id = client, device

    def __repr__(self) -> str:
        return f"<labrat device {self.id}>"

    def describe(self) -> dict:
        return self.client.describe(self.id)

    def get(self, name: str, channel: str | None = None) -> Any:
        return self.client.get(self.id, name, channel)

    def set(self, name: str, value: Any, channel: str | None = None) -> Any:
        return self.client.set(self.id, name, value, channel)

    def call(self, name: str, channel: str | None = None, **args) -> Any:
        return self.client.call(self.id, name, channel, **args)

    def locked(self, minutes: float | None = None):
        return self.client.locked(self.id, minutes)


class DirectDevice(Device):
    """A rat's device reached directly under a lease. The nest still decides who may have it, and its limits travel
    in the lease; the rat checks them. If no address of the rat answers as the real rat, it goes through the nest."""

    def __init__(self, client: Client, device: str, minutes: float = 1.0):
        super().__init__(client, device)
        self.nest = client
        self.lease = client.lease(device, minutes=minutes)
        self.local = device.partition("/")[2]
        self.path = "nest"
        self.url = self._fastest()
        self._stop = threading.Event()
        if self.url is None:                                  # nothing answered: the nest, with a lock, as usual
            client.release_lease(self.lease["id"])
            client.lock(device, minutes)
            self.rat = None
            return
        self.path = self.url
        self.rat = Client(self.url, self.lease["lease"], client.timeout, scheme="Lease")
        threading.Thread(target=self._renew, name=f"labrat-lease-{device}", daemon=True).start()

    def __repr__(self) -> str:
        return f"<labrat device {self.id} via {self.path}>"

    def _fastest(self) -> str | None:
        """The rat's address that answers soonest and proves, with the lease's secret, that it is the rat."""
        from . import lease as leases
        found: list[tuple[float, str]] = []

        def ask(url: str) -> None:
            challenge = secrets.token_hex(16)
            t0 = time.perf_counter()
            try:
                q = f"?challenge={challenge}&lease={self.lease['id']}"
                with urllib.request.urlopen(url + "/api/v1/health" + q, timeout=1.5) as r:
                    h = json.loads(r.read()).get("value") or {}
            except (OSError, ValueError):
                return
            if hmac.compare_digest(str(h.get("lease_proof", "")), leases.proof(self.lease["secret"], challenge)):
                found.append((time.perf_counter() - t0, url))
        threads = [threading.Thread(target=ask, args=(u,), daemon=True) for u in self.lease["urls"]]
        for t in threads:
            t.start()
        for t in threads:
            t.join(2.0)
        return min(found)[1] if found else None

    def _renew(self) -> None:
        every = max(5.0, float(self.lease["minutes"]) * 60 / 3)
        while not self._stop.wait(every):
            try:
                self.lease = {**self.lease, **self.nest.renew_lease(self.lease["id"])}
                self.rat.token = self.lease["lease"]
            except LabratError as e:
                log.warning("cannot renew the lease on %s: %s; it ends by itself", self.id, e.message)

    def _on(self) -> tuple[Client, str]:
        return (self.rat, self.local) if self.rat is not None else (self.nest, self.id)

    def describe(self) -> dict:
        c, d = self._on()
        return c.describe(d)

    def get(self, name: str, channel: str | None = None) -> Any:
        c, d = self._on()
        return c.get(d, name, channel)

    def set(self, name: str, value: Any, channel: str | None = None) -> Any:
        c, d = self._on()
        return c.set(d, name, value, channel)

    def call(self, name: str, channel: str | None = None, **args) -> Any:
        c, d = self._on()
        return c.call(d, name, channel, **args)

    def close(self) -> None:
        """Release the lease (or the lock), leaving the device as it is."""
        self._stop.set()
        try:
            if self.rat is not None:
                self.nest.release_lease(self.lease["id"])
            else:
                self.nest.unlock(self.id)
        except LabratError as e:
            log.warning("releasing %s: %s", self.id, e.message)

    def __enter__(self) -> DirectDevice:
        return self

    def __exit__(self, *exc) -> None:
        self.close()
