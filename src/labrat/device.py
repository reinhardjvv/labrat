"""Devices: what a driver offers, described in one form every client can read.

A device has attributes (values to read and, where allowed, to set), functions (actions that may return data)
and a safe state. Every write passes through Attr.check, which refuses values outside the attribute's range.
A lab's configuration may narrow a range but never widen it: limits protect equipment, so the tighter one wins.

A driver subclasses Device and implements read, write and run. Everything else, including the limit checks,
the safe state and the description, comes from this class.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any

from .message import LabratError

TYPES = {"float": float, "int": int, "bool": bool, "str": str}


@dataclass
class Attr:
    name: str
    type: str = "float"
    unit: str = ""
    access: str = "rw"                       # "r" or "rw"
    min: float | None = None
    max: float | None = None
    choices: list | None = None
    channels: list[str] | None = None        # None: the attribute is not per channel
    doc: str = ""

    def check(self, value: Any) -> Any:
        """The value converted to this attribute's type, or LabratError if it may not be written."""
        if self.access != "rw":
            raise LabratError("forbidden", f"{self.name} is read-only")
        v = convert(self.type, value, self.name)
        if self.choices is not None and v not in self.choices:
            raise LabratError("limit", f"{self.name} {value!r} is not one of {self.choices}")
        if self.type in ("float", "int"):
            if self.min is not None and v < self.min:
                raise LabratError("limit", f"{self.name} {_q(v, self.unit)} is below the limit of {_q(self.min, self.unit)}")
            if self.max is not None and v > self.max:
                raise LabratError("limit", f"{self.name} {_q(v, self.unit)} is above the limit of {_q(self.max, self.unit)}")
        return v

    def narrow(self, lo: float | None = None, hi: float | None = None) -> None:
        """Tighten the range. A looser bound than the current one is ignored."""
        if (lo is not None or hi is not None) and self.type not in ("float", "int"):
            raise LabratError("bad_request", f"{self.name} is a {self.type}: min and max apply only to numbers")
        if lo is not None:
            self.min = lo if self.min is None else max(self.min, lo)
        if hi is not None:
            self.max = hi if self.max is None else min(self.max, hi)
        if self.min is not None and self.max is not None and self.min > self.max:
            raise LabratError("bad_request", f"{self.name}: the limits leave no allowed value ({self.min:g} > {self.max:g})")

    def describe(self) -> dict:
        d = {"type": self.type, "access": self.access}
        for k in ("unit", "min", "max", "choices", "channels", "doc"):
            v = getattr(self, k)
            if v not in (None, "", []):
                d[k] = v
        return d

    @classmethod
    def from_dict(cls, name: str, d: dict) -> Attr:
        known = {k: d[k] for k in ("type", "unit", "access", "min", "max", "choices", "channels", "doc") if k in d}
        if "channels" in known and known["channels"] is not None:
            known["channels"] = [str(c) for c in known["channels"]]
        a = cls(name, **known)
        if a.type not in TYPES:
            raise LabratError("bad_request", f"{name}: type must be one of {', '.join(TYPES)}")
        if a.access not in ("r", "rw"):
            raise LabratError("bad_request", f"{name}: access must be 'r' or 'rw'")
        if (a.min is not None or a.max is not None) and a.type not in ("float", "int"):
            raise LabratError("bad_request", f"{name} is a {a.type}: min and max apply only to numbers")
        return a


@dataclass
class Function:
    name: str
    doc: str = ""
    args: dict[str, str] = field(default_factory=dict)       # argument name -> type
    channels: list[str] | None = None

    def describe(self) -> dict:
        d: dict = {}
        if self.doc:
            d["doc"] = self.doc
        if self.args:
            d["args"] = self.args
        if self.channels:
            d["channels"] = self.channels
        return d


@dataclass
class Stream:
    """Continuous data a device sends while a client listens: camera frames, live readings, waveforms."""
    name: str
    content_type: str
    doc: str = ""
    channels: list[str] | None = None

    def describe(self) -> dict:
        d = {"content_type": self.content_type}
        if self.doc:
            d["doc"] = self.doc
        if self.channels:
            d["channels"] = self.channels
        return d


def _q(v: float, unit: str) -> str:
    return f"{v:g} {unit}".strip()


def convert(kind: str, value: Any, name: str = "value") -> Any:
    """Convert a JSON value to a LABRAT type, refusing anything ambiguous."""
    try:
        if kind == "bool":
            if isinstance(value, bool):
                return value
            if isinstance(value, str) and value.strip().lower() in ("1", "true", "on", "yes"):
                return True
            if isinstance(value, str) and value.strip().lower() in ("0", "false", "off", "no"):
                return False
            if isinstance(value, (int, float)) and value in (0, 1):
                return bool(value)
            raise ValueError
        if kind == "float":
            if isinstance(value, bool):
                raise ValueError
            v = float(value)
            if v != v or v in (float("inf"), float("-inf")):
                raise ValueError
            return v
        if kind == "int":
            if isinstance(value, bool) or (isinstance(value, float) and not value.is_integer()):
                raise ValueError
            return int(value)
        return str(value)
    except (TypeError, ValueError):
        raise LabratError("bad_request", f"{name}: {value!r} is not a valid {kind}") from None


class Device:
    """Base class for drivers. Subclasses fill attrs and functions and implement read, write and run."""

    kind = "generic"
    driver = "generic"

    def __init__(self, name: str, config: dict | None = None):
        self.name = name
        self.config = dict(config or {})
        self.description = str(self.config.get("description", ""))
        self.attrs: dict[str, Attr] = {}
        self.functions: dict[str, Function] = {}
        self.streams: dict[str, Stream] = {}
        self.safe: dict[str, Any] = {}
        self._lock = threading.RLock()

    # -- set up --------------------------------------------------------------------------------------------
    def apply_config(self) -> None:
        """Apply the lab's limits and safe state from the configuration. Call after attrs are filled."""
        for attr, lim in (self.config.get("limits") or {}).items():
            self.limit(attr, lim)
        for attr, value in (self.config.get("safe") or {}).items():
            a = self.attr(attr)
            self.safe[attr] = convert(a.type, value, attr)

    def limit(self, attr: str, lim: dict) -> None:
        if not isinstance(lim, dict):
            raise LabratError("bad_request", f"{self.name}: limits.{attr} must be a table with min and/or max")
        self.attr(attr).narrow(lim.get("min"), lim.get("max"))

    # -- the interface every client uses ---------------------------------------------------------------------
    def attr(self, name: str) -> Attr:
        if name not in self.attrs:
            raise LabratError("not_found", f"{self.name} has no attribute {name!r}; it has {', '.join(self.attrs) or 'none'}")
        return self.attrs[name]

    def _channel(self, channels: list[str] | None, channel: str | None, what: str) -> str | None:
        if channels is None:
            if channel is not None:
                raise LabratError("bad_request", f"{what} is not per channel")
            return None
        if channel is None:
            if len(channels) == 1:
                return channels[0]
            raise LabratError("bad_request", f"{what} needs a channel: one of {', '.join(channels)}")
        if channel not in channels:
            raise LabratError("not_found", f"{what} has no channel {channel!r}; it has {', '.join(channels)}")
        return channel

    def get(self, name: str, channel: str | None = None) -> Any:
        a = self.attr(name)
        ch = self._channel(a.channels, channel, f"{self.name}.{name}")
        with self._lock:
            return convert(a.type, self.read(name, ch), name)

    def set(self, name: str, value: Any, channel: str | None = None) -> Any:
        a = self.attr(name)
        ch = self._channel(a.channels, channel, f"{self.name}.{name}")
        v = a.check(value)
        with self._lock:
            self.write(name, v, ch)
        return v

    def call(self, name: str, args: dict | None = None, channel: str | None = None) -> Any:
        if name not in self.functions:
            raise LabratError("not_found", f"{self.name} has no function {name!r}; it has {', '.join(self.functions) or 'none'}")
        f = self.functions[name]
        ch = self._channel(f.channels, channel, f"{self.name}.{name}")
        args = dict(args or {})
        unknown = set(args) - set(f.args)
        if unknown:
            raise LabratError("bad_request", f"{name} takes {', '.join(f.args) or 'no arguments'}, not {', '.join(sorted(unknown))}")
        typed = {k: convert(f.args[k], v, k) for k, v in args.items()}
        with self._lock:
            return self.run(name, typed, ch)

    def open_stream(self, name: str, channel: str | None = None):
        """The stream's content type and an iterator of byte chunks. Close the iterator to stop it."""
        if name not in self.streams:
            raise LabratError("not_found", f"{self.name} has no stream {name!r}; it has {', '.join(self.streams) or 'none'}")
        st = self.streams[name]
        ch = self._channel(st.channels, channel, f"{self.name}.{name}")
        return st.content_type, self.stream(name, ch)

    def make_safe(self) -> list[str]:
        """Write the safe state on every channel. Returns the problems met, empty when all went well."""
        problems = []
        for name, value in self.safe.items():
            a = self.attrs[name]
            for ch in a.channels or [None]:
                try:
                    with self._lock:
                        self.write(name, value, ch)
                except Exception as e:            # keep going: every other output must still be made safe
                    problems.append(f"{self.name}.{name}{'[' + ch + ']' if ch else ''}: {e}")
        return problems

    def describe(self) -> dict:
        d = {"kind": self.kind, "driver": self.driver,
             "attributes": {n: a.describe() for n, a in self.attrs.items()},
             "functions": {n: f.describe() for n, f in self.functions.items()}}
        if self.streams:
            d["streams"] = {n: st.describe() for n, st in self.streams.items()}
        if self.description:
            d["description"] = self.description
        if self.safe:
            d["safe"] = self.safe
        return d

    def close(self) -> None:
        pass

    # -- for drivers ------------------------------------------------------------------------------------------
    def read(self, name: str, channel: str | None) -> Any:
        raise NotImplementedError

    def write(self, name: str, value: Any, channel: str | None) -> None:
        raise NotImplementedError

    def run(self, name: str, args: dict, channel: str | None) -> Any:
        raise NotImplementedError

    def stream(self, name: str, channel: str | None):
        """A generator of byte chunks for a stream in self.streams."""
        raise NotImplementedError
