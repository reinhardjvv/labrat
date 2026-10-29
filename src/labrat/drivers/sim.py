"""Simulated devices, for trying LABRAT, writing clients and testing without hardware."""
from __future__ import annotations

import json
import time
from typing import Any

from ..device import Attr, Device, Function, Stream
from ..message import LabratError


class SimSupply(Device):
    """A two-channel bench power supply with a resistive load on each output.

    config: channels (default 2), max_voltage (30), max_current (3), load_ohm (100)
    """

    kind = "dc-power"

    def __init__(self, name: str, config: dict | None = None):
        super().__init__(name, config)
        chs = [str(i) for i in range(1, int(self.config.get("channels", 2)) + 1)]
        vmax, imax = float(self.config.get("max_voltage", 30)), float(self.config.get("max_current", 3))
        self.load_ohm = float(self.config.get("load_ohm", 100))
        self.attrs = {
            "VoltageLevel": Attr("VoltageLevel", "float", "V", min=0, max=vmax, channels=chs, doc="output voltage set point"),
            "CurrentLimit": Attr("CurrentLimit", "float", "A", min=0, max=imax, channels=chs, doc="current limit"),
            "OutputEnabled": Attr("OutputEnabled", "bool", channels=chs, doc="output on or off"),
        }
        self.functions = {"Measure": Function("Measure", "measure the output", {"type": "str"}, channels=chs)}
        self.streams = {"Readings": Stream("Readings", "application/x-ndjson", "voltage and current of every channel, 10 per second")}
        self.safe = {"OutputEnabled": False}
        self.state = {ch: {"VoltageLevel": 0.0, "CurrentLimit": 0.1, "OutputEnabled": False} for ch in chs}

    def identify(self):
        return {"manufacturer": "labrat", "model": "SimSupply", "serial": f"sim-{self.name}"}

    def read(self, name, channel):
        return self.state[channel][name]

    def write(self, name, value, channel):
        self.state[channel][name] = value

    def run(self, name, args, channel):
        s = self.state[channel]
        if not s["OutputEnabled"]:
            v, i = 0.0, 0.0
        else:
            i = min(s["VoltageLevel"] / self.load_ohm, s["CurrentLimit"])
            v = i * self.load_ohm
        kind = args.get("type", "voltage")
        if kind not in ("voltage", "current"):
            raise LabratError("bad_request", "Measure type is 'voltage' or 'current'")
        return {"value": v if kind == "voltage" else i, "unit": "V" if kind == "voltage" else "A"}


    def stream(self, name, channel):
        while True:
            row = {"time": round(time.time(), 3)}
            for ch in self.state:
                with self._lock:
                    row[ch] = {"V": self.run("Measure", {"type": "voltage"}, ch)["value"],
                               "A": self.run("Measure", {"type": "current"}, ch)["value"]}
            yield (json.dumps(row) + "\n").encode()
            time.sleep(0.1)


class SimGpio(Device):
    """GPIO lines in memory, configured like the real gpio driver: outputs and inputs by name."""

    kind = "gpio"

    def __init__(self, name: str, config: dict | None = None):
        super().__init__(name, config)
        outs, ins = dict(self.config.get("outputs") or {}), dict(self.config.get("inputs") or {})
        self.attrs = {n: Attr(n, "bool", doc=f"output line {line}") for n, line in outs.items()}
        self.attrs.update({n: Attr(n, "bool", access="r", doc=f"input line {line}") for n, line in ins.items()})
        self.values: dict[str, Any] = {n: False for n in self.attrs}

    def identify(self):
        return {"manufacturer": "labrat", "model": "SimGpio", "serial": f"sim-{self.name}"}

    def read(self, name, channel):
        return self.values[name]

    def write(self, name, value, channel):
        self.values[name] = value
