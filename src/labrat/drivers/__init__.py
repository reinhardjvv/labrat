"""Driver registry. A device's configuration names its driver; this module turns that into a Device.

Built-in drivers are listed in BUILTIN. Other packages add drivers through the 'labrat.drivers' entry point
group, with a name and a Device subclass (see docs/drivers.md):

    [project.entry-points."labrat.drivers"]
    acme-psu = "labrat_acme:AcmePsu"
"""
from __future__ import annotations

import importlib
from importlib.metadata import entry_points

from ..device import Device
from ..message import LabratError

BUILTIN = {
    "sim-supply": "labrat.drivers.sim:SimSupply",
    "sim-gpio": "labrat.drivers.sim:SimGpio",
    "scpi": "labrat.drivers.scpi:Scpi",
    "gpio": "labrat.drivers.gpio:Gpio",
    "camera": "labrat.drivers.camera:Camera",
    "genicam": "labrat.drivers.genicam:Genicam",
    "display": "labrat.drivers.display:Display",
    "serial": "labrat.drivers.serialport:Serial",
    "i2c": "labrat.drivers.i2c:I2c",
    "mqtt": "labrat.drivers.mqtt:Mqtt",
    "http": "labrat.drivers.http:Http",
    "nut": "labrat.drivers.nut:Nut",
    "frames": "labrat.drivers.frames:Frames",
}


def available() -> dict[str, str]:
    """Every driver name this installation knows, with where it comes from."""
    found = dict(BUILTIN)
    for ep in entry_points(group="labrat.drivers"):
        found.setdefault(ep.name, ep.value)
    return found


def load(name: str) -> type[Device]:
    target = available().get(name)
    if target is None:
        raise LabratError("bad_request", f"unknown driver {name!r}; available: {', '.join(sorted(available()))}")
    module, _, attr = target.partition(":")
    cls = getattr(importlib.import_module(module), attr)
    if not (isinstance(cls, type) and issubclass(cls, Device)):
        raise LabratError("bad_request", f"driver {name!r} ({target}) is not a labrat Device")
    return cls


def build(name: str, config: dict, profile=None) -> Device:
    """A ready device from its configuration table, with the lab's limits and safe state applied. A profile given
    here (a draft being tried) is used as it is; otherwise config's profile is looked up in the catalog."""
    if profile is not None:
        pass
    elif config.get("profile"):
        from ..catalog import default, merge
        profile = default().get(str(config["profile"]))
        config = merge(profile, config)
    elif config.get("simulate"):
        raise LabratError("bad_request", f"device {name!r}: simulate needs a profile, whose recordings it answers from")
    if "driver" not in config:
        raise LabratError("bad_request", f"device {name!r} needs a driver or a profile")
    dev = load(str(config["driver"]))(name, config)
    dev.driver = str(config["driver"])
    dev.profile = profile.id if profile else None
    dev.apply_config()
    return dev
