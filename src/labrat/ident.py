"""What a device is, as opposed to what it is called: kind, manufacturer, model, serial and firmware.

A device has an id, the short handle a lab chooses (scope, psu-left, bench-rat/cam), and an identity. The
identity comes from the configuration or, for instruments that answer *IDN?, from the instrument itself. Its
reference, manufacturer/model#serial, names one physical instrument wherever it is plugged in, so two identical
scopes stay apart by their serial numbers.

Selectors find devices the way Spack finds packages:

    oscilloscope                       every oscilloscope
    tektronix/mso54                    every Tektronix MSO54
    oscilloscope tektronix/*           every Tektronix oscilloscope
    #C012345                           the instrument with that serial number
    @1.40                              firmware 1.40
    location=optics* tag=teaching      any identity field, with shell-style patterns
"""
from __future__ import annotations

import difflib
import re
from fnmatch import fnmatch

from .message import LabratError

# kind -> (meaning, IVI class where one exists)
KINDS: dict[str, tuple[str, str]] = {
    "oscilloscope": ("oscilloscope or digitising scope", "IviScope"),
    "function-generator": ("function or arbitrary waveform generator", "IviFgen"),
    "rf-signal-generator": ("RF or microwave signal generator", "IviRFSigGen"),
    "spectrum-analyser": ("spectrum or signal analyser", "IviSpecAn"),
    "vna": ("vector network analyser", ""),
    "dc-power": ("DC power supply", "IviDCPwr"),
    "ac-power": ("AC power source", "IviACPwr"),
    "electronic-load": ("electronic load", ""),
    "source-meter": ("source measure unit", ""),
    "dmm": ("digital multimeter", "IviDmm"),
    "power-meter": ("RF or optical power meter", "IviPwrMeter"),
    "counter": ("frequency counter or timer", "IviCounter"),
    "digitiser": ("digitiser or data acquisition card", "IviDigitizer"),
    "data-logger": ("data logger or multi-channel acquisition unit", ""),
    "switch": ("switch matrix or multiplexer", "IviSwtch"),
    "camera": ("camera", ""),
    "display": ("SLM, DMD, projector or screen used as an instrument", ""),
    "laser": ("laser or laser driver", ""),
    "motion": ("stage, motor or positioner", ""),
    "temperature-controller": ("temperature controller", ""),
    "gpio": ("GPIO lines: relays, shutters, interlocks, switches, sensors", ""),
    "sensor": ("sensor without a more specific kind", ""),
    "ups": ("uninterruptible power supply", ""),
    "light": ("lamp, LED strip or smart light", ""),
    "outlet": ("switched power outlet or smart plug", ""),
    "instrument": ("an instrument whose kind is not set", ""),
}
ALIASES = {
    "scope": "oscilloscope", "dso": "oscilloscope", "mso": "oscilloscope",
    "awg": "function-generator", "fgen": "function-generator", "signal-generator": "function-generator",
    "spectrum-analyzer": "spectrum-analyser", "specan": "spectrum-analyser", "signal-analyser": "spectrum-analyser",
    "signal-analyzer": "spectrum-analyser", "network-analyser": "vna", "network-analyzer": "vna",
    "psu": "dc-power", "power-supply": "dc-power", "load": "electronic-load", "smu": "source-meter",
    "multimeter": "dmm", "digitizer": "digitiser", "daq": "data-logger", "slm": "display", "dmd": "display",
    "projector": "display", "stage": "motion", "relay": "gpio",
    "lamp": "light", "led": "light", "plug": "outlet", "smart-plug": "outlet", "pdu": "outlet",
}
MAKERS = {
    "agilent technologies": "agilent", "keysight technologies": "keysight", "rohde&schwarz": "rohde-schwarz",
    "rohde & schwarz": "rohde-schwarz", "rohde-schwarz": "rohde-schwarz", "r&s": "rohde-schwarz",
    "tektronix, inc.": "tektronix", "rigol technologies": "rigol", "siglent technologies": "siglent",
    "gw instek": "gw-instek", "good will instrument": "gw-instek", "national instruments": "ni",
    "allied vision": "allied-vision", "allied vision technologies": "allied-vision", "teledyne lecroy": "lecroy",
    "keithley instruments": "keithley", "thorlabs inc.": "thorlabs", "stanford research systems": "srs",
}
FIELDS = ("kind", "manufacturer", "model", "serial", "firmware", "location", "tags")


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.strip().lower()).strip("-")


def kind_of(text: str) -> str:
    """The standard kind for a name or alias, or LabratError with the nearest names."""
    k = slug(text)
    k = ALIASES.get(k, k)
    if k not in KINDS:
        near = difflib.get_close_matches(k, list(KINDS) + list(ALIASES), n=3)
        raise LabratError("bad_request", f"unknown kind {text!r}" + (f"; did you mean {', '.join(near)}?" if near else
                                                                       f"; kinds are {', '.join(KINDS)}"))
    return k


def maker(text: str) -> str:
    t = text.strip().lower()
    if t in MAKERS:
        return MAKERS[t]
    for suffix in (" technologies", " instruments", " inc.", " inc", " gmbh", " ltd", " co., ltd.", " corporation"):
        if t.endswith(suffix) and t[: -len(suffix)] in MAKERS.values():
            return t[: -len(suffix)]
    return slug(t)


def parse_idn(reply: str) -> dict:
    """manufacturer, model, serial and firmware from an IEEE 488.2 *IDN? reply."""
    parts = [p.strip() for p in reply.split(",")]
    if len(parts) < 2 or not parts[0]:
        return {}
    out = {"manufacturer": maker(parts[0]), "model": parts[1]}
    if len(parts) > 2 and parts[2] not in ("", "0"):
        out["serial"] = parts[2]
    if len(parts) > 3 and parts[3]:
        out["firmware"] = ",".join(parts[3:])
    return out


def reference(ident: dict) -> str | None:
    """manufacturer/model#serial, when all three are known: one physical instrument."""
    if ident.get("manufacturer") and ident.get("model") and ident.get("serial"):
        return f"{ident['manufacturer']}/{slug(ident['model'])}#{ident['serial']}"
    return None


def from_config(config: dict, default_kind: str) -> dict:
    ident = {"kind": kind_of(config["kind"]) if config.get("kind") else default_kind}
    if config.get("manufacturer"):
        ident["manufacturer"] = maker(str(config["manufacturer"]))
    for k in ("model", "serial", "firmware", "location"):
        if config.get(k):
            ident[k] = str(config[k])
    if config.get("tags"):
        ident["tags"] = [str(t) for t in config["tags"]]
    return ident


# -- selectors ----------------------------------------------------------------------------------------------
def parse_selector(text: str) -> dict[str, str]:
    """Field patterns from a selector string, such as 'oscilloscope tektronix/mso5* #C01*'."""
    sel: dict[str, str] = {}
    for word in text.split():
        if "=" in word:
            k, _, v = word.partition("=")
            k = {"maker": "manufacturer", "tag": "tags", "id": "device"}.get(k, k)
            if k not in (*FIELDS, "device", "rat"):
                raise LabratError("bad_request", f"selector field {k!r}: use {', '.join(FIELDS)}, device or rat")
            sel[k] = v
        elif word.startswith("#"):
            sel["serial"] = word[1:]
        elif word.startswith("@"):
            sel["firmware"] = word[1:]
        elif "/" in word:
            m, _, model = word.partition("/")
            sel["manufacturer"] = maker(m) if "*" not in m else m.lower()
            if model:
                sel["model"] = model
        else:
            try:
                sel["kind"] = kind_of(word)
            except LabratError:
                sel["device"] = word                       # a bare word that is not a kind is a device id pattern
    return sel


def matches(entry: dict, sel: dict[str, str]) -> bool:
    for k, pattern in sel.items():
        v = entry.get(k)
        if k == "tags":
            if not any(fnmatch(t.lower(), pattern.lower()) for t in (v or [])):
                return False
        elif k == "model":
            if v is None or not (fnmatch(str(v).lower(), pattern.lower()) or fnmatch(slug(str(v)), pattern.lower())):
                return False
        elif v is None or not fnmatch(str(v).lower(), pattern.lower()):
            return False
    return True


SORT_KEYS = ("device", "kind", "manufacturer", "model", "serial", "location", "rat")


def sort(entries: list[dict], by: str) -> list[dict]:
    if by not in SORT_KEYS:
        raise LabratError("bad_request", f"sort by one of {', '.join(SORT_KEYS)}")
    return sorted(entries, key=lambda e: (str(e.get(by) or "~"), e.get("device", "")))
