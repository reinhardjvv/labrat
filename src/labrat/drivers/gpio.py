"""GPIO lines on Linux through the character device (/dev/gpiochip*), with libgpiod 2.

    [devices.relays]
    driver = "gpio"
    chip = "/dev/gpiochip0"
    outputs = { heater = 17, lamp = 27 }     # name = line offset
    inputs = { door = 22 }
    safe = { heater = false }                # stop, shutdown and the watchdog switch the heater off

Outputs start inactive. Needs the gpiod package (pip install 'labrat-lab[gpio]') and access to the chip, which on
Raspberry Pi OS means membership of the gpio group.
"""
from __future__ import annotations

from ..device import Attr, Device
from ..message import LabratError


class Gpio(Device):
    kind = "gpio"

    def __init__(self, name: str, config: dict | None = None):
        super().__init__(name, config)
        try:
            import gpiod
            from gpiod.line import Direction, Value
        except ImportError:
            raise LabratError("unavailable", "the gpio driver needs libgpiod 2: pip install 'labrat-lab[gpio]'") from None
        self._value = Value
        chip = str(self.config.get("chip", "/dev/gpiochip0"))
        outs = {str(k): int(v) for k, v in (self.config.get("outputs") or {}).items()}
        ins = {str(k): int(v) for k, v in (self.config.get("inputs") or {}).items()}
        if set(outs) & set(ins):
            raise LabratError("bad_request", f"{name}: a line name is both an output and an input")
        self.lines = {**outs, **ins}
        settings = {line: gpiod.LineSettings(direction=Direction.OUTPUT, output_value=Value.INACTIVE) for line in outs.values()}
        settings.update({line: gpiod.LineSettings(direction=Direction.INPUT) for line in ins.values()})
        try:
            self.request = gpiod.request_lines(chip, consumer=f"labrat:{name}", config=settings)
        except OSError as e:
            raise LabratError("unavailable", f"{name}: cannot open {chip}: {e}") from None
        self.attrs = {n: Attr(n, "bool", doc=f"output line {line}") for n, line in outs.items()}
        self.attrs.update({n: Attr(n, "bool", access="r", doc=f"input line {line}") for n, line in ins.items()})

    def read(self, name, channel):
        return self.request.get_value(self.lines[name]) == self._value.ACTIVE

    def write(self, name, value, channel):
        self.request.set_value(self.lines[name], self._value.ACTIVE if value else self._value.INACTIVE)

    def close(self) -> None:
        self.request.release()
