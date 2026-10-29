"""Helpers shared by the drivers whose attributes are written in the configuration (serial, i2c, mqtt, http).

A template holds {value} and, in functions, {<argument>}. Only those names are replaced, so JSON bodies such as
'{"on": {value}}' need no escaping. A reply is turned into a value in this order: a regular expression
(reply, first group), a JSON path (json, such as "state.bri" or "lights.0.on"), the on and off words for
booleans, and scale and offset for numbers.
"""
from __future__ import annotations

import json
import os
import re
from typing import Any

from ..message import LabratError


def secret(config: dict, key: str, device: str) -> str | None:
    """A password or token from the configuration, or from the environment variable named by <key>_env."""
    if config.get(f"{key}_env"):
        value = os.environ.get(str(config[f"{key}_env"]))
        if value is None:
            raise LabratError("bad_request", f"{device}: the environment variable {config[f'{key}_env']} is not set")
        return value
    return str(config[key]) if config.get(key) is not None else None


def expand_env(text: str, device: str) -> str:
    """${NAME} replaced by the environment variable, so tokens stay out of the configuration file."""
    def one(m):
        if m.group(1) not in os.environ:
            raise LabratError("bad_request", f"{device}: the environment variable {m.group(1)} is not set")
        return os.environ[m.group(1)]
    return re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", one, text)


def check_spec(device: str, name: str, spec: dict) -> dict:
    spec = dict(spec)
    if spec.get("reply"):
        try:
            re.compile(spec["reply"])
        except re.error as e:
            raise LabratError("bad_request", f"{device}.{name}: reply is not a valid regular expression: {e}") from None
    for k in ("scale", "offset"):
        if k in spec and not isinstance(spec[k], (int, float)):
            raise LabratError("bad_request", f"{device}.{name}: {k} is a number")
    return spec


def word(value: Any, spec: dict, on: str = "1", off: str = "0") -> str:
    """A value as text for a template: booleans as the on and off words, numbers in engineering units."""
    if isinstance(value, bool):
        return str(spec.get("on", on)) if value else str(spec.get("off", off))
    if isinstance(value, float) and ("scale" in spec or "offset" in spec):
        value = (value - float(spec.get("offset", 0))) / float(spec.get("scale", 1))
    return str(value)


def fill(template: str, **values) -> str:
    for k, v in values.items():
        template = template.replace("{" + k + "}", str(v))
    return template


def pick(obj: Any, path: str, what: str) -> Any:
    for part in path.split("."):
        try:
            obj = obj[int(part)] if isinstance(obj, list) else obj[part]
        except (KeyError, IndexError, ValueError, TypeError):
            raise LabratError("device", f"{what}: the reply has no {path!r}") from None
    return obj


def parse(raw: Any, spec: dict, kind: str, what: str) -> Any:
    """The value an attribute's reply holds, ready for convert()."""
    if isinstance(raw, bytes):
        raw = raw.decode(errors="replace")
    if spec.get("reply"):
        m = re.search(spec["reply"], str(raw))
        if m is None:
            raise LabratError("device", f"{what}: the reply {str(raw)[:60]!r} does not match {spec['reply']!r}")
        raw = m.group(1) if m.groups() else m.group(0)
    if spec.get("json"):
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except ValueError:
                raise LabratError("device", f"{what}: the reply is not JSON: {raw[:60]!r}") from None
        raw = pick(raw, str(spec["json"]), what)
    if isinstance(raw, str):
        raw = raw.strip()
    if kind == "bool" and ("on" in spec or "off" in spec):
        if str(raw) == str(spec.get("on", "")):
            return True
        if str(raw) == str(spec.get("off", "")):
            return False
    if kind == "bool" and isinstance(raw, str) and raw.upper() in ("ON", "TRUE", "1", "OFF", "FALSE", "0"):
        return raw.upper() in ("ON", "TRUE", "1")
    if kind in ("float", "int") and ("scale" in spec or "offset" in spec):
        try:
            v = float(raw) * float(spec.get("scale", 1)) + float(spec.get("offset", 0))
        except (TypeError, ValueError):
            raise LabratError("device", f"{what}: {raw!r} is not a number") from None
        return round(v) if kind == "int" else v
    return raw
