"""Binary protocols: Modbus RTU and TCP, and the fixed frames many meters, controllers and power supplies use.

    [devices.meter]                        # Modbus: registers, no frames to write
    driver = "frames"
    protocol = "modbus-rtu"                # or modbus-tcp, with address = "tcp://192.0.2.60:502"
    port = "/dev/ttyUSB0"
    baud = 9600
    unit = 1

    [devices.meter.attributes.Voltage]
    register = 0                           # holding register 0 (function 3); input = true for function 4
    type = "u16"                           # u16, i16, u32, i32, f32
    scale = 0.1
    unit = "V"

    [devices.meter.attributes.Relay]
    coil = 5                               # a coil (functions 1 and 5)
    access = "rw"

    [devices.load]                         # fixed frames, written as hex with fields in braces
    driver = "frames"
    port = "/dev/ttyUSB1"
    baud = 115200
    checksum = "sum8"                      # none, sum8, xor8, crc16-modbus or crc16-ccitt, over the bytes before it

    [devices.load.attributes.Current]
    send = "AA 01 01 {checksum}"
    reply_length = 6
    field = { offset = 3, type = "u16be" }
    scale = 0.001
    unit = "A"
    set = "AA 02 01 {value:u16be} {checksum}"

Field types are u8, i8, u16be, u16le, i16be, i16le, u32be, u32le, i32be, i32le, f32be and f32le. A reply's
checksum is checked when check_reply = true. With port = "replay://<profile>" the device answers from a
profile's send_hex and reply_hex recordings.
"""
from __future__ import annotations

import socket
import struct
import threading
import time

from ..device import Attr, Device, Function
from ..message import LabratError

TYPES = {"u8": ">B", "i8": ">b", "u16be": ">H", "u16le": "<H", "i16be": ">h", "i16le": "<h", "u32be": ">I",
         "u32le": "<I", "i32be": ">i", "i32le": "<i", "f32be": ">f", "f32le": "<f"}
MODBUS_TYPES = {"u16": ">H", "i16": ">h", "u32": ">I", "i32": ">i", "f32": ">f"}


def crc16_modbus(data: bytes) -> bytes:
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return struct.pack("<H", crc)


def crc16_ccitt(data: bytes) -> bytes:
    crc = 0xFFFF
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return struct.pack(">H", crc)


CHECKSUMS = {"none": lambda d: b"", "sum8": lambda d: bytes([sum(d) & 0xFF]),
             "xor8": lambda d: bytes([__import__("functools").reduce(lambda a, b: a ^ b, d, 0)]),
             "crc16-modbus": crc16_modbus, "crc16-ccitt": crc16_ccitt}


def build_frame(template: str, checksum: str, value=None, **args) -> bytes:
    """Bytes from a hex template such as 'AA 02 {value:u16be} {checksum}'."""
    out = b""
    for tok in template.split():
        if tok == "{checksum}":
            out += CHECKSUMS[checksum](out)
        elif tok.startswith("{") and tok.endswith("}"):
            name, _, kind = tok[1:-1].partition(":")
            v = value if name == "value" else args.get(name)
            if v is None or kind not in TYPES:
                raise LabratError("bad_request", f"frame field {tok}: no value, or a type other than {', '.join(TYPES)}")
            v = round(v) if not kind.startswith("f") else float(v)
            try:
                out += struct.pack(TYPES[kind], v)
            except struct.error:
                raise LabratError("limit", f"{v} does not fit in a {kind} field") from None
        else:
            try:
                out += bytes.fromhex(tok)
            except ValueError:
                raise LabratError("bad_request", f"frame byte {tok!r} is not hex") from None
    return out


class Link:
    """Bytes to and from a serial port, a TCP socket or a profile's recordings."""

    def __init__(self, cfg: dict, timeout: float):
        self.cfg, self.timeout = cfg, timeout
        self.port = str(cfg.get("port") or cfg.get("address") or "")
        self.ser = self.sock = None
        self.replies: dict[bytes, bytes] | None = None
        self.transcript: list[dict] | None = None
        self.lock = threading.Lock()
        self._pending = b""
        if self.port.startswith("replay://"):
            from ..catalog import default
            recs = default().get(self.port.split("://", 1)[1]).recordings
            self.replies = {bytes.fromhex(r["send_hex"]): bytes.fromhex(r["reply_hex"]) for r in recs if "send_hex" in r}

    def _open(self):
        if self.replies is not None:
            return
        if self.port.startswith("tcp://"):
            if self.sock is None:
                host, _, port = self.port[6:].partition(":")
                self.sock = socket.create_connection((host, int(port or 502)), timeout=self.timeout)
            return
        if self.ser is None:
            try:
                import serial
            except ImportError:
                raise LabratError("unavailable", "serial devices need pyserial: pip install 'labrat-lab[serial]'") from None
            from .serialport import PARITY
            c = self.cfg
            self.ser = serial.serial_for_url(self.port, baudrate=int(c.get("baud", 9600)), timeout=self.timeout,
                                             parity=PARITY[str(c.get("parity", "none"))], stopbits=int(c.get("stop_bits", 1)))
            self.ser.reset_input_buffer()

    def write(self, data: bytes) -> None:
        self._open()
        if self.replies is not None:
            self._pending = self.replies.get(data, b"")
        elif self.sock is not None:
            self.sock.sendall(data)
        else:
            self.ser.write(data)

    def read(self, n: int) -> bytes:
        if self.replies is not None:
            data, self._pending = self._pending[:n], self._pending[n:]
        elif self.sock is not None:
            data, end = b"", time.monotonic() + self.timeout
            while len(data) < n and time.monotonic() < end:
                chunk = self.sock.recv(n - len(data))
                if not chunk:
                    break
                data += chunk
        else:
            data = self.ser.read(n)
        if len(data) < n:
            raise LabratError("device", f"{self.port}: {len(data)} of {n} bytes within the timeout")
        return data

    def exchange(self, frame: bytes, reply_length) -> bytes:
        """Send a frame and read its reply: a length, or a function of the bytes read so far."""
        with self.lock:
            try:
                self.write(frame)
                reply = reply_length(self) if callable(reply_length) else (self.read(int(reply_length)) if reply_length else b"")
            except OSError as e:
                self.close()
                raise LabratError("device", f"{self.port}: {e}") from None
            if self.transcript is not None:
                self.transcript.append({"send_hex": frame.hex(" "), **({"reply_hex": reply.hex(" ")} if reply else {})})
            return reply

    def close(self) -> None:
        for h in (self.ser, self.sock):
            if h is not None:
                try:
                    h.close()
                except OSError:
                    pass
        self.ser = self.sock = None


class Frames(Device):
    kind = "instrument"

    def __init__(self, name: str, config: dict | None = None):
        super().__init__(name, config)
        c = self.config
        self.protocol = str(c.get("protocol", "frames"))
        if self.protocol not in ("frames", "modbus-rtu", "modbus-tcp"):
            raise LabratError("bad_request", f"{name}: protocol is frames, modbus-rtu or modbus-tcp")
        if not (c.get("port") or c.get("address")):
            raise LabratError("bad_request", f"{name}: needs a port (serial or replay://) or an address (tcp://host:port)")
        self.checksum = str(c.get("checksum", "none"))
        if self.checksum not in CHECKSUMS:
            raise LabratError("bad_request", f"{name}: checksum is one of {', '.join(CHECKSUMS)}")
        self.unit = int(c.get("unit", 1))
        self.link = Link(c, float(c.get("timeout", 1.0)))
        self.specs: dict[str, dict] = {}
        self._tid = 0
        for attr, spec in (c.get("attributes") or {}).items():
            spec = dict(spec)
            if self.protocol == "frames":
                if not spec.get("send") and not spec.get("set"):
                    raise LabratError("bad_request", f"{name}.{attr} needs a send frame, a set frame or both")
                f = spec.get("field") or {}
                if spec.get("send") and (f.get("type") not in TYPES or "offset" not in f):
                    raise LabratError("bad_request", f"{name}.{attr}: field is {{ offset = <byte>, type = <one of {', '.join(TYPES)}> }}")
                spec.setdefault("access", "rw" if spec.get("set") else "r")
            else:
                if ("register" in spec) == ("coil" in spec):
                    raise LabratError("bad_request", f"{name}.{attr} needs a register or a coil")
                if "coil" in spec:
                    spec.setdefault("type", "bool")
                elif spec.get("type", "u16") not in MODBUS_TYPES:
                    raise LabratError("bad_request", f"{name}.{attr}: type is one of {', '.join(MODBUS_TYPES)}")
                spec.setdefault("access", "r")
                spec["word"] = spec.get("type", "u16")
                spec["type"] = "bool" if "coil" in spec else "float" if "scale" in spec or spec["word"] == "f32" else "int"
            self.attrs[attr] = Attr.from_dict(attr, spec)
            self.specs[attr] = spec
        self.fns: dict[str, dict] = {}
        for fn, spec in (c.get("functions") or {}).items():
            if not spec.get("send"):
                raise LabratError("bad_request", f"{name}.{fn} needs a send frame")
            self.functions[fn] = Function(fn, spec.get("doc", ""), dict(spec.get("args") or {}))
            self.fns[fn] = dict(spec)

    # -- Modbus ---------------------------------------------------------------------------------------------
    def _modbus(self, pdu: bytes) -> bytes:
        if self.protocol == "modbus-tcp":
            self._tid = (self._tid + 1) & 0xFFFF
            frame = struct.pack(">HHHB", self._tid, 0, len(pdu) + 1, self.unit) + pdu

            def reply(link):
                head = link.read(7)
                return head + link.read(struct.unpack(">H", head[4:6])[0] - 1)
            data = self.link.exchange(frame, reply)[7:]
        else:
            frame = bytes([self.unit]) + pdu
            frame += crc16_modbus(frame)

            def reply(link):
                head = link.read(2)
                if head[1] & 0x80:
                    return head + link.read(3)
                if head[1] in (5, 6, 15, 16):
                    return head + link.read(6)
                n = link.read(1)
                return head + n + link.read(n[0] + 2)
            raw = self.link.exchange(frame, reply)
            if crc16_modbus(raw[:-2]) != raw[-2:]:
                raise LabratError("device", f"{self.name}: the reply's CRC does not match")
            data = raw[1:-2]
        if data[0] & 0x80:
            raise LabratError("device", f"{self.name}: Modbus exception {data[1]} for function {data[0] & 0x7F}")
        return data

    def _words(self, spec: dict) -> int:
        return 2 if spec["word"] in ("u32", "i32", "f32") else 1

    # -- the device -----------------------------------------------------------------------------------------
    def read(self, name, channel):
        spec = self.specs[name]
        if self.protocol == "frames":
            if not spec.get("send"):
                raise LabratError("forbidden", f"{self.name}.{name} cannot be read")
            reply = self.link.exchange(build_frame(spec["send"], self.checksum), spec.get("reply_length"))
            if spec.get("check_reply") and self.checksum != "none":
                n = len(CHECKSUMS[self.checksum](b"\0"))
                if CHECKSUMS[self.checksum](reply[:-n]) != reply[-n:]:
                    raise LabratError("device", f"{self.name}: the reply's checksum does not match")
            f = spec["field"]
            size = struct.calcsize(TYPES[f["type"]])
            raw = struct.unpack(TYPES[f["type"]], reply[int(f["offset"]):int(f["offset"]) + size])[0]
        elif "coil" in spec:
            data = self._modbus(struct.pack(">BHH", 1, int(spec["coil"]), 1))
            return bool(data[2] & 1)
        else:
            fn = 4 if spec.get("input") else 3
            data = self._modbus(struct.pack(">BHH", fn, int(spec["register"]), self._words(spec)))
            raw = struct.unpack(MODBUS_TYPES[spec["word"]], data[2:2 + 2 * self._words(spec)])[0]
        if self.attrs[name].type == "bool":
            return bool(raw)
        v = raw * float(spec.get("scale", 1)) + float(spec.get("offset", 0))
        return round(v) if self.attrs[name].type == "int" else v

    def write(self, name, value, channel):
        spec = self.specs[name]
        counts = (float(value) - float(spec.get("offset", 0))) / float(spec.get("scale", 1))
        if self.protocol == "frames":
            self.link.exchange(build_frame(spec["set"], self.checksum, counts), spec.get("set_reply_length", 0))
        elif "coil" in spec:
            self._modbus(struct.pack(">BHH", 5, int(spec["coil"]), 0xFF00 if value else 0))
        else:
            fmt = MODBUS_TYPES[spec["word"]]
            try:
                data = struct.pack(fmt, counts if spec["word"] == "f32" else round(counts))
            except struct.error:
                raise LabratError("limit", f"{self.name}.{name} {value} does not fit in a {spec['word']} register") from None
            if len(data) == 2:
                self._modbus(struct.pack(">BH", 6, int(spec["register"])) + data)
            else:
                self._modbus(struct.pack(">BHHB", 16, int(spec["register"]), 2, 4) + data)

    def run(self, name, args, channel):
        spec = self.fns[name]
        reply = self.link.exchange(build_frame(spec["send"], self.checksum, **args), spec.get("reply_length", 0))
        return {"hex": reply.hex(" ")} if reply else None

    def close(self) -> None:
        self.link.close()
