"""What a rat can see on the computer it runs on: serial ports, USBTMC instruments and GPIO chips.

Only device files are listed. Nothing is opened or sent, so detection cannot upset an instrument.
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
