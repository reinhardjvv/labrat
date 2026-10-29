"""Probing: asking an unknown device safe questions and keeping every answer.

    labrat probe /dev/ttyUSB0                        # tries the usual baud rates and protocols
    labrat probe hid:///dev/hidraw0 --protocol megatec
    labrat probe tcp://192.0.2.50:5025
    labrat probe http://192.0.2.30
    labrat probe i2c:1                               # the addresses that answer on /dev/i2c-1
    labrat probe /dev/ttyUSB0 --baud 9600 --send "VER?" --send "RAT?"

Probes are read-only: queries ending in '?' and known status queries for each protocol. Words that reset,
reboot, shut down, test or update a device are never sent. Anything else a person or an agent wants to send
waits for someone to approve it, even in god mode, as a wrong guess can damage hardware. Every exchange goes
into a transcript, kept in <state_dir>/probes, which a profile can take as its recordings.
"""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

from .message import LabratError

PACKS = {
    "scpi": ["*IDN?", "SYST:ERR?", "*OPT?"],
    "megatec": ["QMD", "QGS", "QBV", "QMOD", "QVFW", "QPI", "QID", "Q1", "F", "I"],
    "text": ["?", "ID?", "VER?", "*IDN?"],
}
FIRST = {"scpi": "*IDN?", "megatec": "QMD", "text": "?"}          # asked at each baud rate to find one that answers
BAUDS = [9600, 115200, 2400, 19200, 38400, 57600, 4800]
HTTP_PATHS = ["/", "/json/info", "/json/state", "/status", "/rpc/Shelly.GetStatus", "/shelly", "/cm?cmnd=Status%200",
              "/api/config", "/description.xml"]
DENY = re.compile(r"^\*RST|^\*TST|^\*CAL|SYST(?:EM)?:PRES|SYST(?:EM)?:REB|\b(?:RESET|REBOOT|RESTART|FORMAT|ERASE|FLASH|"
                  r"FIRMWARE|UPDATE|UPGRADE|BOOT|DFU|SHUTDOWN|POWEROFF|POWER ?OFF|FACTORY|DEFAULTS?|CALIB\w*|DELETE|WIPE)\b|"
                  r"^S\d|^SN?\d|^T(?:L|\d+)?$|^Q$|^P[EDF][A-Z]$|^C(?:T)?$", re.I)
I2C_HINTS = {0x20: "PCF8574 or MCP23017 port expander", 0x23: "BH1750 light sensor", 0x29: "VL53L0X or TSL2591",
             0x39: "TSL2561 or APDS9960", 0x3C: "SSD1306 display", 0x40: "INA219, HTU21D or PCA9685", 0x44: "SHT3x humidity",
             0x48: "ADS1115 ADC or TMP102 temperature", 0x50: "EEPROM", 0x5C: "AM2320", 0x60: "MCP4725 DAC",
             0x68: "DS3231 clock or MPU6050", 0x76: "BME280 or BMP280", 0x77: "BME280, BMP280 or BMP180"}


def is_query(send: str, protocol: str = "") -> bool:
    """True for what may be sent without asking: queries ending in '?' and the known status queries."""
    s = send.strip()
    if not s or DENY.search(s):
        return False
    return s.endswith("?") or any(s == q for pack in PACKS.values() for q in pack)


def refuse(send: str) -> None:
    if DENY.search(send.strip()):
        raise LabratError("forbidden", f"{send!r} can reset, shut down, test or update a device, so probing never sends it; "
                                       "describe it as a function in a profile if it is really needed")


def printable(text: str) -> bool:
    return bool(text) and sum(c.isprintable() for c in text) / len(text) > 0.9


def open_text_link(target: str, settings: dict | None = None, baud: int | None = None):
    """A line link to a serial port, a HID bridge or a TCP socket, with the given settings."""
    from .drivers.scpi import HidTransport, SerialTransport, TcpTransport
    from .drivers.serialport import EOL, PARITY
    s = dict(settings or {})
    eol = EOL[str(s.get("line_ending", "lf"))]
    read_eol = EOL[str(s.get("reply_ending", s.get("line_ending", "lf")))][-1:]
    timeout = float(s.get("timeout", 1.0))
    if target.startswith("tcp://"):
        return TcpTransport(target, timeout)
    if target.startswith("hid://") or target.startswith("/dev/hidraw"):
        return HidTransport(target if target.startswith("hid://") else f"hid://{target}", timeout, eol, read_eol)
    port = target[len("serial://"):] if target.startswith("serial://") else target
    ser = {"baudrate": int(baud or s.get("baud", 9600)), "parity": PARITY[str(s.get("parity", "none"))]}
    return SerialTransport(f"serial://{port}", timeout, port, ser, eol, read_eol)


class Probe:
    """One probing run on one target, kept as a transcript."""

    def __init__(self, target: str, protocol: str = "auto", sends: list[str] | None = None, baud: int | None = None,
                 line_ending: str | None = None, approved: bool = False):
        self.target, self.protocol, self.baud, self.approved = target, protocol, baud, approved
        self.sends = [str(x) for x in sends or []]
        self.line_ending = line_ending
        for x in self.sends:
            refuse(x)
        self.exchanges: list[dict] = []
        self.notes: list[str] = []
        self.settings: dict = {}

    def needs_approval(self) -> list[str]:
        return [] if self.approved else [x for x in self.sends if not is_query(x)]

    def run(self) -> dict:
        t = self.target
        if t.startswith(("http://", "https://")):
            self._http()
        elif t.startswith("i2c:"):
            self._i2c()
        else:
            self._text()
        from .catalog import default
        cat, found = default(), {}
        for x in self.exchanges:
            reply = x.get("reply") or ""
            for p in cat.by_reply(x.get("send", ""), reply) + (cat.by_idn(reply) if x.get("send") == "*IDN?" else []):
                found[p.id] = p.meta["model"]
            if x.get("send") == "*IDN?" and reply:
                from .ident import parse_idn
                self.notes.append(f"*IDN? says {parse_idn(reply) or reply}")
        return {"target": t, "protocol": self.protocol, "settings": self.settings, "time": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "exchanges": self.exchanges, "notes": self.notes, "profiles": [{"profile": k, "model": v} for k, v in found.items()]}

    # -- text over serial, HID and TCP ----------------------------------------------------------------------
    def _ask(self, link, send: str) -> str | None:
        start = time.monotonic()
        try:
            reply = link.query(send, retry=False)
        except LabratError as e:
            self.exchanges.append({"send": send, "error": e.message.split(": ")[-1], "ms": round((time.monotonic() - start) * 1000)})
            return None
        self.exchanges.append({"send": send, "reply": reply, "ms": round((time.monotonic() - start) * 1000)})
        return reply

    def _text(self) -> None:
        protocols = [self.protocol] if self.protocol != "auto" else ["scpi", "megatec", "text"]
        endings = [self.line_ending] if self.line_ending else ["lf", "cr", "crlf"]
        network = self.target.startswith(("tcp://", "hid://", "/dev/hidraw"))
        bauds = [None] if network else ([self.baud] if self.baud else BAUDS)
        for baud in bauds:
            for proto in protocols:
                for eol in (["cr"] if proto == "megatec" and not self.line_ending else endings):
                    link = open_text_link(self.target, {"line_ending": eol, "timeout": 0.8}, baud)
                    try:
                        try:
                            link.write("")                              # ends any half line an earlier attempt left
                            time.sleep(0.1)
                        except LabratError:
                            pass
                        reply = self._ask(link, FIRST.get(proto, "?"))
                        if reply and printable(reply):
                            self.protocol = proto
                            self.settings = {"line_ending": eol, **({"baud": baud} if baud else {})}
                            for send in [q for q in PACKS[proto] if q != FIRST.get(proto)] + self.sends:
                                self._ask(link, send)
                            return
                    finally:
                        link.close()
                    time.sleep(0.05)
        self.notes.append("nothing answered in a readable form; try --baud, --line-ending or a capture of the vendor's software")
        if self.sends:                                          # still try what was asked for, with the first settings
            link = open_text_link(self.target, {"line_ending": endings[0], "timeout": 1.0}, bauds[0])
            try:
                for send in self.sends:
                    self._ask(link, send)
            finally:
                link.close()

    # -- web APIs -------------------------------------------------------------------------------------------
    def _http(self) -> None:
        base = self.target.rstrip("/")
        self.protocol = "http"
        for path in HTTP_PATHS + [s for s in self.sends if s.startswith("/")]:
            start = time.monotonic()
            try:
                with urllib.request.urlopen(base + path, timeout=3) as r:
                    body = r.read(4096).decode(errors="replace")
                    self.exchanges.append({"send": f"GET {path}", "reply": body, "status": r.status,
                                           "content_type": r.headers.get("Content-Type", ""), "ms": round((time.monotonic() - start) * 1000)})
            except urllib.error.HTTPError as e:
                self.exchanges.append({"send": f"GET {path}", "status": e.code})
            except OSError as e:
                self.exchanges.append({"send": f"GET {path}", "error": str(e)})
                if path == "/":
                    return

    # -- I2C ------------------------------------------------------------------------------------------------
    def _i2c(self) -> None:
        try:
            from smbus2 import SMBus
        except ImportError:
            raise LabratError("unavailable", "probing I2C needs smbus2: pip install 'labrat-lab[i2c]'") from None
        bus = int(self.target.split(":", 1)[1])
        self.protocol = "i2c"
        self.notes.append("each address is read once; a few chips clear a flag when read")
        with SMBus(bus) as b:
            for addr in range(0x03, 0x78):
                try:
                    b.read_byte(addr)
                except OSError:
                    continue
                self.exchanges.append({"address": f"0x{addr:02x}", **({"commonly": I2C_HINTS[addr]} if addr in I2C_HINTS else {})})


def save(transcript: dict, folder: Path) -> Path:
    folder = folder / time.strftime("%Y-%m-%d")
    folder.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r"[^A-Za-z0-9]+", "-", transcript["target"]).strip("-")[-40:]
    path = folder / f"{time.strftime('%H%M%S')}-{slug}.json"
    path.write_text(json.dumps(transcript, indent=2) + "\n", encoding="utf-8")
    transcript["file"] = str(path.relative_to(folder.parent))
    return path
