"""A Python client for a nest or rat.

    from labrat.client import Client
    lab = Client("http://nest.local:8780", token="...")      # or set LABRAT_URL and LABRAT_TOKEN
    lab.set("bench-rat/psu", "VoltageLevel", 3.3, channel="1")
    with lab.locked("bench-rat/psu"):
        lab.set("bench-rat/psu", "OutputEnabled", True, channel="1")
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from contextlib import contextmanager
from typing import Any

from .message import LabratError


class Client:
    def __init__(self, url: str | None = None, token: str | None = None, timeout: float = 30.0):
        self.url = (url or os.environ.get("LABRAT_URL") or "http://127.0.0.1:8780").rstrip("/")
        self.token = token if token is not None else os.environ.get("LABRAT_TOKEN", "")
        self.timeout = timeout

    def send(self, msg: dict | list) -> Any:
        """Send one message (or a list) and return the reply as it came."""
        req = urllib.request.Request(self.url + "/api/v1/message", data=json.dumps(msg).encode(), method="POST",
                                     headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.token}"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            try:
                return json.loads(e.read())
            except ValueError:
                raise LabratError("unavailable", f"{self.url} answered HTTP {e.code}") from None
        except OSError as e:
            raise LabratError("unavailable", f"cannot reach {self.url}: {e}") from None

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
