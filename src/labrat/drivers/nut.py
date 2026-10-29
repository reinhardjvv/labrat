"""Uninterruptible power supplies through Network UPS Tools (NUT), on the nest, a rat or a NAS that runs upsd.

    [devices.ups]
    driver = "nut"
    host = "127.0.0.1"
    port = 3493
    ups = "lab"                            # the name in NUT's ups.conf
    variables = { Temperature = "ups.temperature" }   # more variables, beyond the standard ones
    commands = ["beeper.mute", "test.battery.start.quick"]   # instant commands allowed through Command
    username = "labrat"                    # a upsd.users entry allowed those commands
    password_env = "LAB_UPS_PASSWORD"

Every UPS reads BatteryCharge, BatteryRuntime, Load, InputVoltage, OutputVoltage, Status, OnBattery and
LowBattery. A variable the UPS does not report answers with an error. Instant commands are off unless listed,
so a shutdown command is never one call away by accident. Needs nothing beyond NUT itself.
"""
from __future__ import annotations

import socket

from ..device import Attr, Device, Function
from ..message import LabratError
from .mapped import secret

STANDARD = {
    "BatteryCharge": ("battery.charge", "float", "%"),
    "BatteryRuntime": ("battery.runtime", "float", "s"),
    "Load": ("ups.load", "float", "%"),
    "InputVoltage": ("input.voltage", "float", "V"),
    "OutputVoltage": ("output.voltage", "float", "V"),
    "Status": ("ups.status", "str", ""),
}


def _unquote(text: str) -> str:
    text = text.strip()
    if text.startswith('"') and text.endswith('"'):
        text = text[1:-1]
    return text.replace('\\"', '"').replace("\\\\", "\\")


class Nut(Device):
    kind = "ups"

    def __init__(self, name: str, config: dict | None = None):
        super().__init__(name, config)
        c = self.config
        self.host, self.port = str(c.get("host", "127.0.0.1")), int(c.get("port", 3493))
        self.ups = str(c.get("ups", ""))
        if not self.ups or any(ch.isspace() or ch == '"' for ch in self.ups):
            raise LabratError("bad_request", f"{name}: ups is the UPS's name in NUT's ups.conf")
        self.timeout = float(c.get("timeout", 3.0))
        self.vars = {n: v[0] for n, v in STANDARD.items()}
        for n, (_, kind, unit) in STANDARD.items():
            self.attrs[n] = Attr(n, kind, unit, "r", doc=self.vars[n])
        for n, var in (c.get("variables") or {}).items():
            self.vars[n] = str(var)
            self.attrs[n] = Attr(n, "str", access="r", doc=str(var))
        self.attrs["OnBattery"] = Attr("OnBattery", "bool", access="r", doc="ups.status contains OB")
        self.attrs["LowBattery"] = Attr("LowBattery", "bool", access="r", doc="ups.status contains LB")
        self.allowed = [str(x) for x in c.get("commands") or []]
        for cmd in self.allowed:
            if not cmd or any(ch.isspace() for ch in cmd):
                raise LabratError("bad_request", f"{name}: instant command {cmd!r} has spaces")
        self.functions["Variables"] = Function("Variables", "every variable the UPS reports")
        if self.allowed:
            self.functions["Command"] = Function("Command", f"an instant command: {', '.join(self.allowed)}", {"name": "str"})
        self.user = str(c["username"]) if c.get("username") else None
        self.password = secret(c, "password", name)

    # -- the NUT protocol -----------------------------------------------------------------------------------
    def _talk(self, lines: list[str], until: str | None = None) -> list[str]:
        """Send lines, one reply line each, and for a LIST read until its END line."""
        try:
            with socket.create_connection((self.host, self.port), timeout=self.timeout) as s, s.makefile("rwb") as f:
                replies = []
                for line in lines:
                    f.write(line.encode() + b"\n")
                    f.flush()
                    reply = f.readline().decode(errors="replace").rstrip("\n")
                    if reply.startswith("ERR"):
                        raise LabratError("device", f"{self.name}: {line.split()[0]} answered {reply}")
                    replies.append(reply)
                if until:
                    while True:
                        reply = f.readline().decode(errors="replace").rstrip("\n")
                        if not reply or reply.startswith(until):
                            break
                        replies.append(reply)
                f.write(b"LOGOUT\n")
                f.flush()
                return replies
        except OSError as e:
            raise LabratError("unavailable", f"{self.name}: cannot reach upsd at {self.host}:{self.port}: {e}") from None

    def variable(self, var: str) -> str:
        reply = self._talk([f"GET VAR {self.ups} {var}"])[0]
        prefix = f"VAR {self.ups} {var} "
        if not reply.startswith(prefix):
            raise LabratError("device", f"{self.name}: unexpected reply {reply[:60]!r}")
        return _unquote(reply[len(prefix):])

    def variables(self) -> dict[str, str]:
        out = {}
        for line in self._talk([f"LIST VAR {self.ups}"], until=f"END LIST VAR {self.ups}")[1:]:
            parts = line.split(" ", 3)
            if len(parts) == 4 and parts[0] == "VAR":
                out[parts[2]] = _unquote(parts[3])
        return out

    def read(self, name, channel):
        if name in ("OnBattery", "LowBattery"):
            return ("OB" if name == "OnBattery" else "LB") in self.variable("ups.status").split()
        return self.variable(self.vars[name])

    def run(self, name, args, channel):
        if name == "Variables":
            return self.variables()
        cmd = str(args.get("name", ""))
        if cmd not in self.allowed:
            raise LabratError("forbidden", f"{self.name}: {cmd!r} is not an allowed command; allowed: {', '.join(self.allowed)}")
        login = [f"USERNAME {self.user}", f"PASSWORD {self.password}"] if self.user else []
        self._talk(login + [f"INSTCMD {self.ups} {cmd}"])
        return None

    def identify(self) -> dict:
        v = self.variables()
        from ..ident import maker
        out = {"manufacturer": v.get("device.mfr") or v.get("ups.mfr"), "model": v.get("device.model") or v.get("ups.model"),
               "serial": v.get("device.serial") or v.get("ups.serial"), "firmware": v.get("ups.firmware")}
        if out["manufacturer"]:
            out["manufacturer"] = maker(out["manufacturer"])
        return {k: x for k, x in out.items() if x}
