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
            raise LabratError(err.get("code", "internal"), err.get("message", "no reason given"))
        return reply.get("value")

    def health(self) -> dict:
        with urllib.request.urlopen(self.url + "/api/v1/health", timeout=self.timeout) as r:
            return json.loads(r.read())["value"]

    def devices(self) -> list[dict]:
        return self.request("list")

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

    def rats(self) -> list[dict]:
        return self.request("rats")

    def adopt(self, name: str, key: str, url: str | None = None) -> dict:
        return self.request("adopt", name=name, value=key, args={"url": url} if url else None)

    def forget(self, name: str) -> dict:
        return self.request("forget", name=name)
