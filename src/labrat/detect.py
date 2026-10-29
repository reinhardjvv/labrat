"""What a rat can see on the computer it runs on: USB devices, serial ports, USBTMC instruments, GPIO chips
and I2C buses, with the catalog profiles that fit them.

Listing opens and sends nothing, so it cannot upset an instrument. Only scan with probe sends anything: the
read-only query a matching profile names to confirm the model.
"""
from __future__ import annotations

import glob
import os


def resources() -> dict[str, list[str]]:
    by_id = sorted(glob.glob("/dev/serial/by-id/*"))           # names that survive replugging
    seen = {os.path.realpath(p) for p in by_id}
    plain = [p for p in sorted(glob.glob("/dev/ttyUSB*") + glob.glob("/dev/ttyACM*")) if p not in seen]
    return {"serial": by_id + plain, "usbtmc": sorted(glob.glob("/dev/usbtmc*")),
            "gpio": sorted(glob.glob("/dev/gpiochip*"))}


SYS = "/sys"


def usb_devices(sys_root: str | None = None) -> list[dict]:
    """USB devices with their vendor:product, names, serial and the device files they bring (tty, hidraw, usbtmc)."""
    root = sys_root or SYS
    nodes: dict[str, list[str]] = {}
    for cls, pattern in (("tty", "ttyUSB*"), ("tty", "ttyACM*"), ("hidraw", "hidraw*"), ("usbmisc", "usbtmc*")):
        for n in glob.glob(f"{root}/class/{cls}/{pattern}"):
            try:
                nodes[os.path.realpath(f"{n}/device")] = nodes.get(os.path.realpath(f"{n}/device"), []) + ["/dev/" + os.path.basename(n)]
            except OSError:
                continue
    out = []
    for d in sorted(glob.glob(f"{root}/bus/usb/devices/*")):
        if not os.path.isfile(f"{d}/idVendor"):
            continue

        def read(name, d=d):
            try:
                with open(f"{d}/{name}") as f:
                    return f.read().strip()
            except OSError:
                return ""
        real = os.path.realpath(d)
        files = sorted({f for path, fs in nodes.items() if path.startswith(real + "/") or path == real for f in fs})
        out.append({"usb": f"{read('idVendor')}:{read('idProduct')}".lower(), "manufacturer": read("manufacturer"),
                    "product": read("product"), "serial": read("serial"), "bus": read("busnum"), "address": read("devnum"),
                    "files": files})
    return [u for u in out if not u["usb"].startswith("1d6b:")]          # root hubs


def scan(hub=None, probe: bool = False) -> list[dict]:
    """What is plugged in, with the profiles that fit each USB device. With probe, a profile's own read-only
    match query is sent to confirm it. Ports already serving a device are left alone."""
    from .catalog import default
    cat = default()
    used = set()
    for d in (hub.devices.values() if hub else []):
        for k in ("port", "address", "chip"):
            v = str(d.config.get(k, ""))
            if v:
                used.add(v.split("://")[-1].split("?")[0])
    out = []
    for u in usb_devices():
        row = {**u, "in_use": [f for f in u["files"] if f in used], "profiles": []}
        for p in cat.by_usb(u["usb"]):
            cand = {"profile": p.id, "model": p.meta["model"], "confirmed": None}
            if probe and not row["in_use"]:
                cand["confirmed"] = _confirm(p, u["files"])
            row["profiles"].append(cand)
        out.append(row)
    found = resources()
    listed = {f for u in out for f in u["files"]}
    for kind in ("serial", "usbtmc", "gpio"):
        for f in found[kind]:
            if os.path.realpath(f) not in listed and f not in listed:
                out.append({"usb": "", "product": kind, "files": [f], "in_use": [f] if f in used else [], "profiles": []})
    for bus in sorted(glob.glob("/dev/i2c-*")):
        out.append({"usb": "", "product": "I2C bus", "files": [bus], "in_use": [], "profiles": []})
    return out


def _confirm(profile, files: list[str]) -> bool | None:
    """Send a profile's match query on the file it would use. True or False, or None when it has no query."""
    m = next((m for m in profile.matches if m.get("probe")), None)
    if m is None:
        return None
    from .probe import open_text_link
    for f in files:
        if f.startswith("/dev/hidraw") or f.startswith("/dev/tty"):
            try:
                link = open_text_link(f, profile.device)
                try:
                    import re
                    return bool(re.search(str(m["reply"]), link.query(m["probe"])))
                finally:
                    link.close()
            except Exception:
                continue
    return False


def rat_config(name: str, extra: str = "") -> str:
    """A starting rat.toml with a commented example for every resource found."""
    found = resources()
    lines = [
        "# LABRAT rat configuration. Reference: docs/configuration.md",
        f'name = "{name}"',
        "port = 8781",
        "watchdog_s = 30          # safe state after this long without a request, 0 turns it off",
        *([extra.rstrip()] if extra else []),
        "",
        "# A simulated supply, so the rat has something to serve straight away. Remove it when real devices are in.",
        "[devices.sim]",
        'driver = "sim-supply"',
        "",
    ]
    for i, port in enumerate(found["serial"], 1):
        lines += [f"# [devices.serial{i}]", '# driver = "scpi"', f'# address = "serial://{port}?baud=9600"', ""]
    for i, dev in enumerate(found["usbtmc"], 1):
        lines += [f"# [devices.usb{i}]", '# driver = "scpi"', f'# address = "usbtmc://{dev}"', ""]
    for chip in found["gpio"][:1]:
        lines += ["# [devices.gpio]", '# driver = "gpio"', f'# chip = "{chip}"', "# outputs = { relay1 = 17 }",
                  "# inputs = { button = 27 }", "# safe = { relay1 = false }", ""]
    if not any(found.values()):
        lines += ["# No serial ports, USBTMC instruments or GPIO chips were found on this computer.", ""]
    return "\n".join(lines)
