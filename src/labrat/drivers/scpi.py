"""Any instrument that speaks text commands (SCPI and similar), described entirely in the configuration.

    [devices.psu]
    driver = "scpi"
    address = "tcp://192.0.2.10:5025"        # or serial:///dev/ttyUSB0?baud=9600, usbtmc:///dev/usbtmc0
    safe_commands = ["OUTP OFF"]             # sent by stop, on shutdown and by the rat's watchdog

    [devices.psu.attributes.VoltageLevel]
    type = "float"
    unit = "V"
    min = 0
    max = 12
    get = "VOLT?"
    set = "VOLT {value}"

    [devices.psu.functions.Identify]
    query = "*IDN?"

Commands may use {value} and, for per-channel attributes, {channel}. Raw commands are off unless raw is set to
"query" (only commands ending in '?') or "on". LABRAT cannot check a raw command against the limits, and some
queries change an instrument too (READ? can switch an output on, *TST? runs a self-test), so describe the
queries you need as functions and leave raw off where you can.
"""
from __future__ import annotations

import os
import socket
import threading
from typing import Any
from urllib.parse import parse_qs, urlparse

from ..device import Attr, Device, Function
from ..message import LabratError

FORBIDDEN = ("\n", "\r", ";")


class Transport:
    """A line-based link to an instrument: write a line, or write a line and read one back."""

    def __init__(self, address: str, timeout: float = 2.0):
        self.address, self.timeout = address, timeout
        self._io_lock = threading.Lock()

    def write(self, line: str) -> None:
        """Not retried: a write that may have arrived is not sent twice."""
        with self._io_lock:
            try:
                self._send(line)
            except (OSError, TimeoutError) as e:
                self.close()
                raise LabratError("device", f"{self.address}: {e}") from None

    def query(self, line: str) -> str:
        with self._io_lock:
            return self._retry(lambda: (self._send(line), self._receive())[1])

    def _retry(self, op):
        """Run op; after a broken link, reconnect once and run it again."""
        try:
            return op()
        except (OSError, TimeoutError):
            self.close()                       # a late reply must not answer the next query: start clean
            try:
                return op()
            except (OSError, TimeoutError) as e:
                self.close()
                raise LabratError("device", f"{self.address}: {e}") from None

    def _send(self, line: str) -> None:
        raise NotImplementedError

    def _receive(self) -> str:
        raise NotImplementedError

    def close(self) -> None:
        pass


class TcpTransport(Transport):
    def __init__(self, address: str, timeout: float = 2.0):
        super().__init__(address, timeout)
        u = urlparse(address)
        if not u.hostname or not u.port:
            raise LabratError("bad_request", f"{address}: a TCP address is tcp://host:port")
        self.host, self.port = u.hostname, u.port
        self.sock: socket.socket | None = None
        self.buf = b""

    def _conn(self) -> socket.socket:
        if self.sock is None:
            self.sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
            self.buf = b""
        return self.sock

    def _send(self, line: str) -> None:
        self._conn().sendall(line.encode() + b"\n")

    def _receive(self) -> str:
        s = self._conn()
        while b"\n" not in self.buf:
            chunk = s.recv(65536)
            if not chunk:
                raise ConnectionError("connection closed by the instrument")
            self.buf += chunk
        line, _, self.buf = self.buf.partition(b"\n")
        return line.decode(errors="replace").strip()

    def close(self) -> None:
        if self.sock is not None:
            try:
                self.sock.close()
            finally:
                self.sock = None


class SerialTransport(Transport):
    def __init__(self, address: str, timeout: float = 2.0):
        super().__init__(address, timeout)
        u = urlparse(address)
        q = {k: v[-1] for k, v in parse_qs(u.query).items()}
        self.port, self.baud = u.path, int(q.get("baud", 9600))
        self.ser = None

    def _conn(self):
        if self.ser is None:
            try:
                import serial
            except ImportError:
                raise LabratError("unavailable", "serial instruments need pyserial: pip install 'labrat-lab[serial]'") from None
            self.ser = serial.Serial(self.port, self.baud, timeout=self.timeout)
            self.ser.reset_input_buffer()
        return self.ser

    def _send(self, line: str) -> None:
        self._conn().write(line.encode() + b"\n")

    def _receive(self) -> str:
        raw = self._conn().readline()
        if not raw.endswith(b"\n"):
            raise TimeoutError("no reply within the timeout")
        return raw.decode(errors="replace").strip()

    def close(self) -> None:
        if self.ser is not None:
            try:
                self.ser.close()
            finally:
                self.ser = None


class UsbtmcTransport(Transport):
    """A USBTMC instrument through the Linux kernel driver (/dev/usbtmc*)."""

    def __init__(self, address: str, timeout: float = 2.0):
        super().__init__(address, timeout)
        self.path = urlparse(address).path
        self.fd: int | None = None

    def _conn(self) -> int:
        if self.fd is None:
            self.fd = os.open(self.path, os.O_RDWR)
        return self.fd

    def _send(self, line: str) -> None:
        os.write(self._conn(), line.encode() + b"\n")

    def _receive(self) -> str:
        return os.read(self._conn(), 1 << 20).decode(errors="replace").strip()

    def close(self) -> None:
        if self.fd is not None:
            try:
                os.close(self.fd)
            finally:
                self.fd = None


TRANSPORTS = {"tcp": TcpTransport, "serial": SerialTransport, "usbtmc": UsbtmcTransport}


def transport(address: str, timeout: float = 2.0) -> Transport:
    scheme = urlparse(address).scheme
    if scheme not in TRANSPORTS:
        raise LabratError("bad_request", f"{address}: the address must start with {', '.join(s + '://' for s in TRANSPORTS)}")
    return TRANSPORTS[scheme](address, timeout)


def _clean(text: str, what: str) -> str:
    if any(c in text for c in FORBIDDEN):
        raise LabratError("bad_request", f"{what} may not contain a newline or ';'")
    return text


class Scpi(Device):
    kind = "scpi"

    def __init__(self, name: str, config: dict | None = None):
        super().__init__(name, config)
        if "address" not in self.config:
            raise LabratError("bad_request", f"{name}: an scpi device needs an address")
        self.link = transport(str(self.config["address"]), float(self.config.get("timeout", 2.0)))
        self.raw = str(self.config.get("raw", "off"))
        if self.raw not in ("off", "query", "on"):
            raise LabratError("bad_request", f"{name}: raw is 'off', 'query' or 'on'")
        self.safe_commands = [_clean(str(c), "a safe command") for c in self.config.get("safe_commands", [])]
        self.commands: dict[str, dict] = {}
        for attr_name, spec in (self.config.get("attributes") or {}).items():
            spec = dict(spec)
            if "get" not in spec and "set" not in spec:
                raise LabratError("bad_request", f"{name}.{attr_name} needs a get or a set command")
            spec.setdefault("access", "rw" if "set" in spec else "r")
            self.attrs[attr_name] = Attr.from_dict(attr_name, spec)
            self.commands[attr_name] = spec
        for fn, spec in (self.config.get("functions") or {}).items():
            spec = dict(spec)
            if ("query" in spec) == ("send" in spec):
                raise LabratError("bad_request", f"{name}.{fn} needs exactly one of query or send")
            chs = [str(c) for c in spec["channels"]] if spec.get("channels") else None
            self.functions[fn] = Function(fn, spec.get("doc", ""), dict(spec.get("args") or {}), chs)
            self.commands["()" + fn] = spec
        if self.raw != "off":
            self.functions["Query"] = Function("Query", "send a raw command ending in '?' and return the reply", {"command": "str"})
        if self.raw == "on":
            self.functions["Write"] = Function("Write", "send a raw command; the limits cannot check it", {"command": "str"})

    def _fmt(self, template: str, value: Any = None, channel: str | None = None, **args) -> str:
        spec_bool = None
        if isinstance(value, bool):
            spec_bool = value
        if spec_bool is not None:
            value = "1" if spec_bool else "0"
        if isinstance(value, str):
            _clean(value, "a value")
        for k, v in args.items():
            if isinstance(v, str):
                _clean(v, k)
        return template.format(value=value, channel=channel, **args)

    def read(self, name, channel):
        spec = self.commands[name]
        if "get" not in spec:
            raise LabratError("forbidden", f"{self.name}.{name} cannot be read")
        reply = self.link.query(self._fmt(spec["get"], channel=channel))
        if self.attrs[name].type == "bool":
            return reply.strip().upper() in ("1", "ON", "TRUE")
        return reply

    def write(self, name, value, channel):
        spec = self.commands[name]
        if isinstance(value, bool):
            value = spec.get("on", "1") if value else spec.get("off", "0")
        self.link.write(self._fmt(spec["set"], value, channel))

    def run(self, name, args, channel):
        if name in ("Query", "Write") and "()" + name not in self.commands:
            cmd = _clean(args.get("command", ""), "command")
            if name == "Query":
                if not cmd.rstrip().endswith("?"):
                    raise LabratError("forbidden", "Query sends only commands ending in '?'")
                return self.link.query(cmd)
            self.link.write(cmd)
            return None
        spec = self.commands["()" + name]
        if "query" in spec:
            return self.link.query(self._fmt(spec["query"], channel=channel, **args))
        self.link.write(self._fmt(spec["send"], channel=channel, **args))
        return None

    def make_safe(self) -> list[str]:
        problems = []
        for cmd in self.safe_commands:
            try:
                self.link.write(cmd)
            except Exception as e:
                problems.append(f"{self.name}: {cmd}: {e}")
        return problems + super().make_safe()

    def close(self) -> None:
        self.link.close()
