"""Anything with a web API: smart plugs and lights (Shelly, WLED, Hue), network sensors, lab gadgets, services.

    [devices.strip]
    driver = "http"
    kind = "light"
    url = "http://192.0.2.30"
    headers = { Authorization = "Bearer ${STRIP_TOKEN}" }   # ${NAME} comes from the environment
    safe = { On = false }

    [devices.strip.attributes.On]
    type = "bool"
    get = "/json/state"                    # GET, and the field in the JSON reply
    json = "on"
    set = "POST /json/state"               # method and path; the body follows
    body = '{"on": {value}}'

    [devices.strip.attributes.Brightness]
    type = "int"
    min = 0
    max = 255
    get = "/json/state"
    json = "bri"
    set = "POST /json/state"
    body = '{"bri": {value}}'

    [devices.strip.functions.Preset]
    request = "POST /json/state"
    body = '{"ps": {id}}'
    args = { id = "int" }

Booleans are written as true and false unless on and off say otherwise. A reply is parsed with json, or with a
regular expression in reply for plain text. A function returns the reply, as JSON where it is JSON. HTTPS
certificates are checked unless verify = false.
"""
from __future__ import annotations

import json
import ssl
import urllib.error
import urllib.request

from ..device import Attr, Device, Function
from ..message import LabratError
from .mapped import check_spec, expand_env, fill, parse, word

METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")


def _route(text: str, default: str, what: str) -> tuple[str, str]:
    parts = str(text).split(None, 1)
    method, path = (parts[0].upper(), parts[1]) if len(parts) == 2 else (default, parts[0])
    if method not in METHODS:
        raise LabratError("bad_request", f"{what}: the method is one of {', '.join(METHODS)}")
    if not path.startswith("/"):
        raise LabratError("bad_request", f"{what}: the path starts with '/'")
    return method, path


class Http(Device):
    kind = "instrument"

    def __init__(self, name: str, config: dict | None = None):
        super().__init__(name, config)
        c = self.config
        self.url = str(c.get("url", "")).rstrip("/")
        if not self.url.startswith(("http://", "https://")):
            raise LabratError("bad_request", f"{name}: url is http://... or https://...")
        self.timeout = float(c.get("timeout", 5.0))
        self.headers = {str(k): expand_env(str(v), name) for k, v in (c.get("headers") or {}).items()}
        self.context = None if c.get("verify", True) else ssl._create_unverified_context()
        self.specs: dict[str, dict] = {}
        for attr_name, spec in (c.get("attributes") or {}).items():
            spec = check_spec(name, attr_name, spec)
            if not spec.get("get") and not spec.get("set"):
                raise LabratError("bad_request", f"{name}.{attr_name} needs a get, a set or both")
            for k, default in (("get", "GET"), ("set", "POST")):
                if spec.get(k):
                    spec[k] = _route(spec[k], default, f"{name}.{attr_name}.{k}")
            spec.setdefault("access", "rw" if spec.get("set") else "r")
            self.attrs[attr_name] = Attr.from_dict(attr_name, spec)
            self.specs[attr_name] = spec
        self.fns: dict[str, dict] = {}
        for fn, spec in (c.get("functions") or {}).items():
            spec = check_spec(name, fn, spec)
            if not spec.get("request"):
                raise LabratError("bad_request", f"{name}.{fn} needs a request, such as \"POST /api/run\"")
            spec["request"] = _route(spec["request"], "POST", f"{name}.{fn}")
            self.functions[fn] = Function(fn, spec.get("doc", ""), dict(spec.get("args") or {}))
            self.fns[fn] = spec

    def _send(self, method: str, path: str, body: str | None, content_type: str = "application/json") -> bytes:
        headers = dict(self.headers)
        if body is not None:
            headers.setdefault("Content-Type", content_type)
        req = urllib.request.Request(self.url + path, data=body.encode() if body is not None else None, method=method,
                                     headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout, context=self.context) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            raise LabratError("device", f"{self.name}: {method} {path} answered HTTP {e.code}") from None
        except OSError as e:
            raise LabratError("unavailable", f"{self.name}: cannot reach {self.url}: {e}") from None

    def read(self, name, channel):
        spec = self.specs[name]
        if not spec.get("get"):
            raise LabratError("forbidden", f"{self.name}.{name} cannot be read")
        method, path = spec["get"]
        return parse(self._send(method, path, None), spec, self.attrs[name].type, f"{self.name}.{name}")

    def write(self, name, value, channel):
        spec = self.specs[name]
        method, path = spec["set"]
        v = word(value, spec, "true", "false")
        body = fill(str(spec.get("body", "{value}")), value=v)
        self._send(method, fill(path, value=v), body if method != "GET" else None, str(spec.get("content_type", "application/json")))

    def run(self, name, args, channel):
        spec = self.fns[name]
        method, path = spec["request"]
        words = {k: word(v, spec, "true", "false") for k, v in args.items()}
        body = fill(str(spec["body"]), **words) if spec.get("body") is not None else None
        reply = self._send(method, fill(path, **words), body, str(spec.get("content_type", "application/json")))
        if spec.get("reply") or spec.get("json"):
            return parse(reply, spec, "str", f"{self.name}.{name}")
        text = reply.decode(errors="replace")
        try:
            return json.loads(text) if text.strip() else None
        except ValueError:
            return text
