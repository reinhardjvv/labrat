"""Anything on an MQTT broker: smart lights and plugs (Zigbee2MQTT, Tasmota, Shelly), sensors, ESP32 boards.

    [devices.lamp]
    driver = "mqtt"
    kind = "light"
    broker = "mqtt://192.0.2.10:1883"      # mqtts:// for TLS
    username = "labrat"
    password_env = "LAB_MQTT_PASSWORD"     # or password = "...", kept out of the file where possible
    safe = { Power = false }

    [devices.lamp.attributes.Power]
    type = "bool"
    state = "zigbee2mqtt/lamp"             # topic the device reports on
    json = "state"                         # the field in its JSON message
    command = "zigbee2mqtt/lamp/set"       # topic to change it
    payload = '{"state": "{value}"}'
    on = "ON"
    off = "OFF"

    [devices.lamp.attributes.Brightness]
    type = "int"
    min = 0
    max = 254
    state = "zigbee2mqtt/lamp"
    json = "brightness"
    command = "zigbee2mqtt/lamp/set"
    payload = '{"brightness": {value}}'

    [devices.lamp.functions.Blink]
    topic = "zigbee2mqtt/lamp/set"
    payload = '{"effect": "blink"}'

A read returns the last message on the state topic, waiting up to timeout seconds for the first one. An
attribute with only a command topic reads back the value last written. Needs paho-mqtt
(pip install 'labrat-lab[mqtt]').
"""
from __future__ import annotations

import logging
import threading
from urllib.parse import urlparse

from ..device import Attr, Device, Function
from ..message import LabratError
from .mapped import check_spec, fill, parse, secret, word

log = logging.getLogger("labrat")


class Mqtt(Device):
    kind = "instrument"

    def __init__(self, name: str, config: dict | None = None):
        super().__init__(name, config)
        try:
            import paho.mqtt.client as paho
        except ImportError:
            raise LabratError("unavailable", "the mqtt driver needs paho-mqtt: pip install 'labrat-lab[mqtt]'") from None
        c = self.config
        u = urlparse(str(c.get("broker", "")))
        if u.scheme not in ("mqtt", "mqtts") or not u.hostname:
            raise LabratError("bad_request", f"{name}: broker is mqtt://host[:port] or mqtts://host[:port]")
        self.timeout = float(c.get("timeout", 5.0))
        self.specs: dict[str, dict] = {}
        self.last: dict[str, object] = {}                  # topic -> last payload
        self.written: dict[str, object] = {}
        self._new = threading.Condition()
        for attr_name, spec in (c.get("attributes") or {}).items():
            spec = check_spec(name, attr_name, spec)
            if not spec.get("state") and not spec.get("command"):
                raise LabratError("bad_request", f"{name}.{attr_name} needs a state topic, a command topic or both")
            spec.setdefault("access", "rw" if spec.get("command") else "r")
            self.attrs[attr_name] = Attr.from_dict(attr_name, spec)
            self.specs[attr_name] = spec
        self.fns: dict[str, dict] = {}
        for fn, spec in (c.get("functions") or {}).items():
            if not spec.get("topic"):
                raise LabratError("bad_request", f"{name}.{fn} needs a topic")
            self.functions[fn] = Function(fn, spec.get("doc", ""), dict(spec.get("args") or {}))
            self.fns[fn] = dict(spec)
        self.topics = sorted({s["state"] for s in self.specs.values() if s.get("state")})

        self.client = paho.Client(paho.CallbackAPIVersion.VERSION2, client_id=str(c.get("client_id", f"labrat-{name}")))
        user = c.get("username")
        if user:
            self.client.username_pw_set(str(user), secret(c, "password", name))
        if u.scheme == "mqtts":
            self.client.tls_set()
        self.client.on_connect = self._connected
        self.client.on_message = self._message
        self.client.reconnect_delay_set(1, 30)
        self.client.connect_async(u.hostname, u.port or (8883 if u.scheme == "mqtts" else 1883))
        self.client.loop_start()                           # reconnects by itself when the broker comes back

    def _connected(self, client, userdata, flags, reason, properties=None):
        for t in self.topics:
            client.subscribe(t)

    def _message(self, client, userdata, msg):
        with self._new:
            self.last[msg.topic] = msg.payload
            self._new.notify_all()

    def read(self, name, channel):
        spec = self.specs[name]
        if not spec.get("state"):
            if name not in self.written:
                raise LabratError("unavailable", f"{self.name}.{name} has no state topic and has not been written yet")
            return self.written[name]
        with self._new:
            if not self._new.wait_for(lambda: spec["state"] in self.last, self.timeout):
                raise LabratError("unavailable", f"{self.name}: no message on {spec['state']} within {self.timeout:g} s")
            payload = self.last[spec["state"]]
        return parse(payload, spec, self.attrs[name].type, f"{self.name}.{name}")

    def _publish(self, topic: str, payload: str, spec: dict) -> None:
        info = self.client.publish(topic, payload, qos=int(spec.get("qos", 0)), retain=bool(spec.get("retain", False)))
        if info.rc != 0:
            raise LabratError("unavailable", f"{self.name}: cannot publish to {topic} (the broker is not connected)")
        if int(spec.get("qos", 0)) > 0:
            info.wait_for_publish(self.timeout)

    def write(self, name, value, channel):
        spec = self.specs[name]
        self._publish(spec["command"], fill(str(spec.get("payload", "{value}")), value=word(value, spec, "true", "false")), spec)
        self.written[name] = value

    def run(self, name, args, channel):
        spec = self.fns[name]
        self._publish(spec["topic"], fill(str(spec.get("payload", "")), **{k: word(v, spec, "true", "false") for k, v in args.items()}), spec)
        return None

    def close(self) -> None:
        self.client.loop_stop()
        self.client.disconnect()
