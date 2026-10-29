"""Captures: what a vendor's own software says to a device, recorded while it works.

    labrat capture start serial /dev/ttyUSB0 --baud 9600
        point the vendor software at the port this prints (a link to a pseudo-terminal in <state_dir>/captures);
        LABRAT passes every byte through to the device and back, and keeps both directions
    labrat capture start usb 0665:5161
        reads the kernel's USB monitor (usbmon) for that device, so nothing sits in between; needs root, or the
        usbmon module with read access to /sys/kernel/debug/usb/usbmon
    labrat capture stop

A capture ends as a transcript in <state_dir>/captures, in the same form as a probe's: what was sent, what came
back, and when. A profile can take it as its recordings. Text that looks like lines is kept as text, anything
else as hex.
"""
from __future__ import annotations

import glob
import json
import os
import re
import select
import threading
import time
from pathlib import Path

from .message import LabratError

USBMON = "/sys/kernel/debug/usb/usbmon"


def _entry(direction: str, data: bytes) -> dict:
    try:
        text = data.decode("ascii")
        if all(c.isprintable() or c in "\r\n\t" for c in text):
            return {"dir": direction, "text": text}
    except UnicodeDecodeError:
        pass
    return {"dir": direction, "hex": data.hex(" ")}


def exchanges(events: list[dict]) -> list[dict]:
    """Pairs of what the host sent and what the device answered, from a list of directional chunks. Chunks in a
    row in one direction (a reply in 8-byte HID reports, say) are one message."""
    joined: list[dict] = []
    for e in events:
        last = joined[-1] if joined else None
        if last and last["dir"] == e["dir"] and ("text" in last) == ("text" in e) and e["t"] - last["t_end"] < 0.5:
            if "text" in e:
                last["text"] += e["text"]
            else:
                last["hex"] += " " + e["hex"]
            last["t_end"] = e["t"]
        else:
            joined.append({**e, "t_end": e["t"]})
    out: list[dict] = []
    for e in joined:
        if e["dir"] == "out":
            out.append({"send" if "text" in e else "send_hex": e.get("text", "").strip("\r\n") if "text" in e else e["hex"],
                        "t": e["t"]})
        elif out and "reply" not in out[-1] and "reply_hex" not in out[-1]:
            if "text" in e and "send" in out[-1]:
                out[-1]["reply"] = e["text"].strip("\r\n")
            else:
                out[-1]["reply_hex"] = e.get("hex") or e["text"].encode().hex(" ")
        else:
            out.append({"reply" if "text" in e else "reply_hex": e.get("text", e.get("hex")), "t": e["t"], "unsolicited": True})
    return out


class Capture:
    def __init__(self, kind: str, target: str, folder: Path, baud: int = 9600, link: str | None = None):
        self.kind, self.target, self.folder = kind, target, folder
        self.events: list[dict] = []
        self.started = time.time()
        self.stopping = threading.Event()
        self.error: str | None = None
        self.link = None
        if kind == "serial":
            self._open_serial(baud, link)
        elif kind == "usb":
            self._open_usb()
        else:
            raise LabratError("bad_request", "a capture is serial or usb")
        self.thread = threading.Thread(target=self._run, name=f"labrat-capture-{kind}", daemon=True)
        self.thread.start()

    # -- serial pass-through --------------------------------------------------------------------------------
    def _open_serial(self, baud: int, link: str | None) -> None:
        try:
            import serial
        except ImportError:
            raise LabratError("unavailable", "serial captures need pyserial: pip install 'labrat-lab[serial]'") from None
        try:
            self.dev = serial.serial_for_url(self.target, baudrate=baud, timeout=0)
        except (OSError, ValueError) as e:
            raise LabratError("unavailable", f"cannot open {self.target}: {e}") from None
        import tty
        self.master, slave = os.openpty()
        tty.setraw(slave)
        self.pty = os.ttyname(slave)
        self.folder.mkdir(parents=True, exist_ok=True)
        self.link = link or str(self.folder / f"tty-{os.path.basename(self.target)}")
        try:
            if os.path.islink(self.link):
                os.unlink(self.link)
            os.symlink(self.pty, self.link)
        except OSError:
            self.link = self.pty

    def _serial(self) -> None:
        fd = self.dev.fileno()
        while not self.stopping.is_set():
            ready = select.select([self.master, fd], [], [], 0.2)[0]
            if self.master in ready:
                data = os.read(self.master, 4096)
                self.dev.write(data)
                self._add("out", data)
            if fd in ready:
                data = self.dev.read(4096)
                if data:
                    os.write(self.master, data)
                    self._add("in", data)

    # -- usbmon ---------------------------------------------------------------------------------------------
    def _open_usb(self) -> None:
        from .detect import usb_devices
        dev = next((u for u in usb_devices() if u["usb"] == self.target.lower()), None)
        if dev is None:
            raise LabratError("not_found", f"no USB device {self.target} is plugged in")
        path = f"{USBMON}/{dev['bus']}u"
        if not os.path.exists(path):
            raise LabratError("unavailable", f"{path} is missing: run 'sudo modprobe usbmon' and make sure debugfs is mounted")
        try:
            self.mon = open(path, "rb", buffering=0)
        except PermissionError:
            raise LabratError("forbidden", f"reading {path} needs root; run the rat as root for the capture, or "
                                           "give its user read access to usbmon") from None
        self.address = int(dev["address"])

    def _usb(self) -> None:
        buf = b""
        while not self.stopping.is_set():
            if not select.select([self.mon], [], [], 0.2)[0]:
                continue
            buf += os.read(self.mon.fileno(), 65536)
            *lines, buf = buf.split(b"\n")
            for line in lines:
                e = parse_usbmon(line.decode(errors="replace"), self.address)
                if e:
                    self._add(*e)

    # -- both -----------------------------------------------------------------------------------------------
    def _add(self, direction: str, data: bytes) -> None:
        if data:
            self.events.append({**_entry(direction, data), "t": round(time.time() - self.started, 4)})

    def _run(self) -> None:
        try:
            self._serial() if self.kind == "serial" else self._usb()
        except Exception as e:                                  # the device was unplugged, most likely
            self.error = str(e)

    def status(self) -> dict:
        out = {"capture": self.kind, "target": self.target, "running": self.thread.is_alive(), "chunks": len(self.events),
               "seconds": round(time.time() - self.started)}
        if self.link:
            out["connect_to"] = self.link
        if self.error:
            out["error"] = self.error
        return out

    def stop(self) -> dict:
        self.stopping.set()
        self.thread.join(timeout=3)
        for closer in ("dev", "mon"):
            h = getattr(self, closer, None)
            if h is not None:
                h.close()
        if self.kind == "serial":
            os.close(self.master)
            if self.link and os.path.islink(self.link):
                os.unlink(self.link)
        transcript = {"target": self.target, "protocol": f"capture-{self.kind}", "time": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                      "seconds": round(time.time() - self.started, 1), "events": self.events, "exchanges": exchanges(self.events)}
        folder = self.folder / time.strftime("%Y-%m-%d")
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{time.strftime('%H%M%S')}-{self.kind}-{re.sub(r'[^A-Za-z0-9]+', '-', self.target).strip('-')}.json"
        path.write_text(json.dumps(transcript, indent=2) + "\n", encoding="utf-8")
        return {**self.status(), "running": False, "file": str(path.relative_to(self.folder)),
                "exchanges": len(transcript["exchanges"])}


def parse_usbmon(line: str, address: int) -> tuple[str, bytes] | None:
    """('out' or 'in', data) from one usbmon text line for the device at this address, or None.

    A line reads: tag timestamp event type:bus:device:endpoint status length = data words. Out data is taken
    where it is submitted (S), in data where it completes (C)."""
    parts = line.split()
    if len(parts) < 6 or "=" not in parts:
        return None
    m = re.fullmatch(r"([CZIB])([io]):(\d+):(\d+):(\d+)", parts[3])
    if not m or int(m.group(4)) != address or m.group(1) == "C" and m.group(5) == "0":
        return None                                             # another device, or setup on the control pipe
    event, direction = parts[2], m.group(2)
    if (direction == "o" and event != "S") or (direction == "i" and event != "C"):
        return None
    words = parts[parts.index("=") + 1:]
    try:
        data = bytes.fromhex("".join(words))
    except ValueError:
        return None
    if m.group(1) == "I" and direction == "o" and data[:1] == b"\x00":
        data = data[1:]                                         # a HID report id
    data = data.rstrip(b"\x00") if m.group(1) == "I" else data
    return ("out" if direction == "o" else "in", data)


def captures_available() -> dict:
    return {"serial": True, "usb": bool(glob.glob(f"{USBMON}/*u"))}
