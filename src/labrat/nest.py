"""The nest: the server a lab talks to. It serves its own devices and those of the rats it has adopted.

Devices on a rat appear as '<rat>/<device>'. The nest checks every change against its own limits before passing
it on, and the rat checks it again against its own. A lock taken at the nest covers the device wherever it is.
The nest keeps adopted rats in <state_dir>/rats.json (readable by its owner only, as it holds the rats' keys),
checks each one every heartbeat_s seconds, and follows a rat to a new address when the network gives it one.
"""
from __future__ import annotations

import copy
import hmac
import json
import logging
import os
import secrets
import threading
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import config as conf
from .device import Attr
from .discovery import Browser
from .drivers import build
from .hub import Hub, Principal
from .message import LabratError, Message, ok, role_allows
from .rat import fingerprint, proof

log = logging.getLogger("labrat")


class RatLink:
    """The nest's connection to one adopted rat."""

    def __init__(self, name: str, url: str, key: str, fingerprint: str = ""):
        self.name, self.url, self.key, self.fingerprint = name, url.rstrip("/"), key, fingerprint
        self.online = False
        self.described: dict[str, dict] = {}

    def send(self, msg: dict, timeout: float = 10.0) -> dict:
        req = urllib.request.Request(self.url + "/api/v1/message", data=json.dumps(msg).encode(), method="POST",
                                     headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.key}"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                reply = json.loads(r.read())
        except urllib.error.HTTPError as e:
            try:
                reply = json.loads(e.read())
            except ValueError:
                raise LabratError("unavailable", f"rat {self.name} answered HTTP {e.code}") from None
        except (OSError, ValueError) as e:
            self.online = False
            raise LabratError("unavailable", f"rat {self.name} is not reachable at {self.url}: {e}") from None
        self.online = True
        return reply

    def verify(self, url: str, timeout: float = 5.0) -> bool:
        """True when the rat at url proves it holds this link's key. The key itself is not sent."""
        challenge = secrets.token_hex(16)
        try:
            with urllib.request.urlopen(f"{url.rstrip('/')}/api/v1/health?challenge={challenge}", timeout=timeout) as r:
                h = json.loads(r.read()).get("value") or {}
        except (OSError, ValueError, AttributeError):
            return False
        return isinstance(h, dict) and h.get("role") == "rat" and hmac.compare_digest(str(h.get("proof", "")), proof(self.key, challenge))

    def ask(self, msg: dict, timeout: float = 10.0):
        """The value of a reply, or the rat's error raised as a LabratError."""
        reply = self.send(msg, timeout)
        if not reply.get("ok"):
            err = reply.get("error") or {}
            raise LabratError(err.get("code", "device"), f"{self.name}: {err.get('message', 'no reason given')}")
        return reply.get("value")

    def open_stream(self, device: str, name: str, channel: str | None):
        q = f"?channel={urllib.parse.quote(channel)}" if channel else ""
        path = f"/api/v1/streams/{urllib.parse.quote(device)}/{urllib.parse.quote(name)}{q}"
        req = urllib.request.Request(self.url + path, headers={"Authorization": f"Bearer {self.key}"})
        try:
            r = urllib.request.urlopen(req, timeout=10)
        except urllib.error.HTTPError as e:
            try:
                err = json.loads(e.read()).get("error") or {}
            except ValueError:
                err = {}
            raise LabratError(err.get("code", "device"), f"{self.name}: {err.get('message', f'HTTP {e.code}')}") from None
        except OSError as e:
            raise LabratError("unavailable", f"rat {self.name} is not reachable at {self.url}: {e}") from None

        def chunks():
            with r:
                while True:
                    data = r.read1(65536)
                    if not data:
                        return
                    yield data
        return r.headers.get("Content-Type", "application/octet-stream"), chunks()

    def to_state(self) -> dict:
        return {"url": self.url, "key": self.key, "fingerprint": self.fingerprint}


class Nest(Hub):
    role = "nest"

    def __init__(self, cfg: dict, devices: dict | None = None, browse: bool | None = None):
        from .catalog import configure
        configure(cfg.get("catalog") or [Path(cfg["state_dir"]) / "profiles"])
        if devices is None:
            devices = {n: build(n, d) for n, d in cfg["devices"].items()}
        state = Path(cfg["state_dir"])
        super().__init__(cfg["name"], devices, conf.tokens(cfg), state / "audit.jsonl", float(cfg["lock_minutes"]),
                         cfg.get("approvals"), float(cfg.get("approval_minutes", 30)), cfg.get("require_lock"))
        from .users import Users
        self.users = Users(self, state / "users.json")
        from .bench import Workbench
        self.bench = Workbench(self, state)                           # first: it brings back devices added while running
        self.limits: dict = cfg.get("limits") or {}
        for dev_id, attrs in self.limits.items():
            if "/" not in dev_id and dev_id not in self.devices:
                raise LabratError("bad_request", f"limits for {dev_id!r}: the nest has no such device (a rat's is '<rat>/<device>')")
            for attr, lim in attrs.items():
                if not isinstance(lim, dict) or not set(lim) <= {"min", "max"}:
                    raise LabratError("bad_request", f"limits.{dev_id}.{attr} takes min and max")
                if dev_id in self.devices:
                    self.devices[dev_id].limit(attr, lim)
        if cfg.get("stores") or cfg.get("recordings") or cfg.get("experiments"):
            from .record import Recorder
            from .store import stores_from
            self.recorder = Recorder(self, stores_from(cfg), cfg.get("recordings") or {})
            if cfg.get("experiments"):
                from .experiment import Experiments
                self.experiments = Experiments(self, self.recorder.stores, cfg["experiments"], state / "experiments.json")
        self.state_file = state / "rats.json"
        self.rats: dict[str, RatLink] = {}
        if self.state_file.is_file():
            for name, r in json.loads(self.state_file.read_text()).items():
                self.rats[name] = RatLink(name, r["url"], r["key"], r.get("fingerprint", ""))
        self.browser = Browser() if (cfg.get("discovery", True) if browse is None else browse) else None
        self.heartbeat_s = float(cfg.get("heartbeat_s", 5.0))
        self._stop = threading.Event()
        threading.Thread(target=self._heartbeat, name="labrat-heartbeat", daemon=True).start()

    # -- rats -----------------------------------------------------------------------------------------------
    def _save(self) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_file.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump({n: r.to_state() for n, r in self.rats.items()}, f, indent=2)
        os.replace(tmp, self.state_file)

    def _heartbeat(self) -> None:
        while not self._stop.wait(self.heartbeat_s):
            for rat in list(self.rats.values()):
                self._follow(rat)
                try:
                    rat.ask({"op": "health"}, timeout=3)
                except LabratError:
                    pass

    def _follow(self, rat: RatLink) -> None:
        """Move to a new address discovery reports for this rat, once the rat there proves it holds the key."""
        if self.browser is None or rat.online:
            return
        for f in self.browser.snapshot("rat"):
            if f.name == rat.name and f.fingerprint == rat.fingerprint and f.url != rat.url and rat.verify(f.url):
                log.info("rat %s moved from %s to %s", rat.name, rat.url, f.url)
                rat.url = f.url
                self._save()

    def rat_list(self) -> list[dict]:
        out = [{"name": r.name, "url": r.url, "adopted": True, "online": r.online, "fingerprint": r.fingerprint}
               for r in self.rats.values()]
        for f in self.browser.snapshot("rat") if self.browser else []:
            if f.name not in self.rats:
                out.append({**f.to_dict(), "adopted": False})
        return out

    def adopt(self, name: str, key: str | None, url: str | None) -> dict:
        conf.check_name(name, "rat")
        if not key:
            raise LabratError("bad_request", "adopt needs the rat's key as value (on the rat: labrat rat key)")
        if not url:
            seen = [f for f in (self.browser.snapshot("rat") if self.browser else []) if f.name == name]
            if not seen:
                raise LabratError("not_found", f"no rat called {name!r} on the network; give its url in args")
            url = seen[0].url
        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            raise LabratError("bad_request", "args.url is the rat's address, such as http://192.0.2.20:8781")
        link = RatLink(name, url, key, fingerprint(key))
        if not link.verify(url):                         # before the key is sent anywhere
            raise LabratError("unauthorised", f"the rat at {url} could not prove it holds that key")
        health = link.ask({"op": "health"})
        if not isinstance(health, dict) or health.get("fingerprint") != link.fingerprint:
            raise LabratError("unauthorised", f"the rat at {url} does not have that key")
        self.rats[name] = link
        self._save()
        log.info("adopted rat %s at %s", name, url)
        return {"name": name, "url": url, "fingerprint": link.fingerprint, "devices": [d["device"] for d in link.ask({"op": "list"})]}

    def forget(self, name: str) -> dict:
        if name not in self.rats:
            raise LabratError("not_found", f"no adopted rat {name!r}")
        del self.rats[name]
        self._save()
        return {"name": name, "adopted": False}

    def _remote(self, device: str) -> tuple[RatLink, str]:
        rat, _, local = device.partition("/")
        if rat not in self.rats:
            raise LabratError("not_found", f"no adopted rat {rat!r}")
        return self.rats[rat], local

    def open_stream(self, device: str, name: str, channel: str | None, who: Principal):
        if "/" not in device:
            return super().open_stream(device, name, channel, who)
        if not role_allows(who.role, "get"):
            raise LabratError("forbidden", f"{who.name} may not watch streams")
        rat, local = self._remote(device)
        return rat.open_stream(local, name, channel)

    def exists(self, device: str) -> None:
        if "/" in device:
            self._describe_remote(device)
        else:
            self.device(device)

    def _describe_remote(self, device: str) -> dict:
        """The rat's description, with this nest's limits merged in, so clients see the range that applies."""
        rat, local = self._remote(device)
        d = rat.ask({"op": "describe", "device": local})
        if not isinstance(d, dict):
            raise LabratError("device", f"{rat.name} sent a description that is not an object")
        rat.described[local] = copy.deepcopy(d)             # the rat's own ranges, merged afresh for every change
        attrs = d.get("attributes") or {}
        for name, lim in (self.limits.get(device) or {}).items():
            if name not in attrs:
                log.warning("nest limit for %s.%s: the device has no such attribute", device, name)
                continue
            a = Attr.from_dict(name, attrs[name])
            a.narrow(lim.get("min"), lim.get("max"))
            attrs[name] = a.describe()
        return d

    def _remote_attr(self, device: str, name: str) -> Attr:
        """The rat's description of an attribute, narrowed by this nest's own limits."""
        rat, local = self._remote(device)
        d = rat.described.get(local) or self._describe_remote(device)
        spec = (d.get("attributes") or {}).get(name)
        if spec is None:
            raise LabratError("not_found", f"{device} has no attribute {name!r}")
        a = Attr.from_dict(name, spec)
        lim = (self.limits.get(device) or {}).get(name)
        if lim:
            a.narrow(lim.get("min"), lim.get("max"))
        return a

    # -- messages -------------------------------------------------------------------------------------------
    def dispatch(self, m: Message, who: Principal) -> dict:
        if m.op == "rats":
            return ok(self.rat_list())
        if m.op == "adopt":
            return ok(self.adopt(m.name, None if m.value is None else str(m.value), m.args.get("url")))
        if m.op == "forget":
            return ok(self.forget(m.name))
        if m.op in ("scan", "probe", "capture", "profile", "device"):
            return self._bench(m, who)
        if m.device is None or "/" not in m.device:
            return super().dispatch(m, who)
        rat, local = self._remote(m.device)
        if m.op == "describe":
            return ok(self._describe_remote(m.device))
        if m.op in ("lock", "unlock", "who", "users"):
            return super().dispatch(m, who)
        fwd = {**m.to_dict(), "device": local}
        fwd.pop("id", None)
        args = {k: v for k, v in (fwd.pop("args", None) or {}).items() if k != "god"}   # never passed on from a client
        if m.op == "set" and who.god:
            args["god"] = True
        if args:
            fwd["args"] = args
        if m.op == "get":
            reply = rat.send(fwd)
        else:
            self.check_lock(m.device, who)
            if m.op == "set":
                fwd["value"] = self._remote_attr(m.device, m.name).check(m.value, who.god)
            reply = rat.send(fwd)
        if not reply.get("ok"):
            err = reply.get("error") or {}
            raise LabratError(err.get("code", "device"), f"{rat.name}: {err.get('message', 'no reason given')}")
        reply.pop("id", None)
        return reply

    def _bench(self, m: Message, who: Principal) -> dict:
        """Workbench operations run where the device is plugged in: here, or on the rat in args.rat (or named in a
        device id such as bench-rat/ups). The nest checks the role and approvals first and the rat trusts it."""
        rat_name = m.args.get("rat")
        name = m.name
        if m.op == "device" and name and "/" in name:
            rat_name, _, name = name.partition("/")
        if not rat_name:
            return super().dispatch(m, who)
        from .bench import check_role
        check_role(m, who)
        if m.op == "profile" and m.args.get("action") == "save":
            from .catalog import parse
            if parse(str(m.args.get("toml", ""))).meta["status"] == "tested" and who.role != "admin":
                raise LabratError("forbidden", "only an admin marks a profile tested; save it as status = \"draft\"")
        args = {k: v for k, v in m.args.items() if k not in ("rat", "approved")}
        if m.op == "probe":
            from .probe import Probe
            if Probe(str(args.get("target", "")), sends=args.get("sends")).needs_approval() and not m.approved:
                raise self.ask_approval(m, who)
            if m.approved:
                args["approved"] = True
        if rat_name not in self.rats:
            raise LabratError("not_found", f"no adopted rat {rat_name!r}")
        fwd = {"op": m.op, **({"name": name} if name else {}), "args": args}
        return ok(self.rats[rat_name].ask(fwd, timeout=120))

    def entries(self) -> list[dict]:
        out = super().entries()
        for rat in list(self.rats.values()):
            try:
                for d in rat.ask({"op": "list"}, timeout=3):
                    dev_id = f"{rat.name}/{d['device']}"
                    d.pop("locked_by", None)
                    holder = self.holder(dev_id)
                    out.append({**d, "device": dev_id, "rat": rat.name, **({"locked_by": holder} if holder else {})})
            except LabratError:
                out.append({"device": f"{rat.name}/*", "rat": rat.name, "online": False})
        return out

    def stop(self, who: Principal | None = None, reason: str = "") -> dict:
        """The safe state here and on every rat at the same time, so a slow instrument delays nobody."""
        msg = {"op": "stop", "args": {"reason": f"from nest {self.name}: {reason}".strip()}}

        def one(rat: RatLink) -> list[str]:
            try:
                value = rat.ask(msg, timeout=5)
                return [f"{rat.name}: {p}" for p in (value or {}).get("problems", [])]
            except LabratError as e:
                return [str(e)]
        rats = list(self.rats.values())
        with ThreadPoolExecutor(max_workers=min(16, len(rats) + 1)) as pool:
            futures = [pool.submit(one, r) for r in rats]
            problems = list(Hub.stop(self, who, reason)["problems"])
            for f in futures:
                problems += f.result()
        return {"safe": not problems, "problems": problems}

    def close(self) -> None:
        self._stop.set()
        if self.browser is not None:
            self.browser.close()
        super().close()
