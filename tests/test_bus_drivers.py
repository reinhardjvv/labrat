import json
import socket
import sys
import threading
import types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from labrat.drivers import build
from labrat.message import LabratError


def line_server(answer):
    """A TCP device that answers each line with answer(line), or says nothing when it returns None."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen()
    seen = []

    def serve():
        while True:
            try:
                conn, _ = srv.accept()
            except OSError:
                return
            with conn:
                f = conn.makefile("rwb")
                for raw in f:
                    line = raw.decode().rstrip("\r\n")
                    seen.append((raw, line))
                    out = answer(line)
                    if out is not None:
                        f.write(out.encode())
                        f.flush()
    threading.Thread(target=serve, daemon=True).start()
    return srv, srv.getsockname()[1], seen


# -- serial -----------------------------------------------------------------------------------------------------
def test_serial_device_with_its_own_commands(tmp_path):
    pytest.importorskip("serial")
    state = {"rate": 1.5, "run": "NO"}

    def answer(line):
        if line == "RAT?":
            return f"RAT={state['rate']}\r"
        if line.startswith("RAT "):
            state["rate"] = float(line[4:])
            return None
        if line == "STATUS":
            return f"OK RUN={state['run']}\r"
        if line in ("YES", "NO"):
            state["run"] = line
        return None
    srv, port, seen = line_server(answer)
    dev = build("pump", {"driver": "serial", "port": f"socket://127.0.0.1:{port}", "line_ending": "crlf", "reply_ending": "cr",
                         "attributes": {"Rate": {"type": "float", "unit": "ml/min", "min": 0, "max": 50, "get": "RAT?",
                                                 "reply": "RAT=([-0-9.]+)", "set": "RAT {value}"},
                                        "Running": {"type": "bool", "get": "STATUS", "reply": r"RUN=(\w+)", "on": "YES", "off": "NO",
                                                    "set": "{value}"}},
                         "safe": {"Running": False}})
    try:
        assert dev.get("Rate") == 1.5
        dev.set("Rate", 12)
        assert dev.get("Rate") == 12.0
        with pytest.raises(LabratError) as e:
            dev.set("Rate", 60)
        assert e.value.code == "limit"
        dev.set("Running", True)
        assert dev.get("Running") is True
        assert dev.make_safe() == [] and dev.get("Running") is False
        assert seen[0][0].endswith(b"\r\n")
        assert dev.identity() == {"kind": "instrument"}                         # no *IDN? sent
        assert not any(line == "*IDN?" for _, line in seen)
    finally:
        dev.close()
        srv.close()


# -- i2c --------------------------------------------------------------------------------------------------------
class FakeBus:
    def __init__(self, bus):
        self.regs = {0x00: 0x19, 0x01: 0x40, 0x40: 0, 0x41: 0, 0x10: 0b1010_0000}
        self.byte = 0xFF

    def read_byte(self, addr):
        return self.byte

    def write_byte(self, addr, v):
        self.byte = v

    def read_byte_data(self, addr, reg):
        return self.regs.get(reg, 0)

    def write_byte_data(self, addr, reg, v):
        self.regs[reg] = v

    def read_i2c_block_data(self, addr, reg, n):
        return [self.regs.get(reg + i, 0) for i in range(n)]

    def write_i2c_block_data(self, addr, reg, data):
        for i, b in enumerate(data):
            self.regs[reg + i] = b

    def close(self):
        pass


@pytest.fixture
def smbus(monkeypatch):
    monkeypatch.setitem(sys.modules, "smbus2", types.SimpleNamespace(SMBus=FakeBus))


def test_i2c_registers_fields_and_scaling(smbus):
    dev = build("climate", {"driver": "i2c", "address": 0x48, "init": [[0x20, 0x07]], "raw": "read", "attributes": {
        "Temperature": {"register": 0x00, "size": 2, "signed": True, "shift": 4, "bits": 12, "scale": 0.0625, "unit": "°C"},
        "Output": {"register": 0x40, "size": 2, "scale": 0.001, "unit": "V", "min": 0, "max": 3.3, "access": "rw"},
        "Mode": {"register": 0x10, "shift": 4, "bits": 2, "access": "rw"},
        "Alert": {"register": 0x10, "shift": 7, "bits": 1},
        "Port": {"access": "rw"}}})
    assert dev.bus.regs[0x20] == 0x07                                          # init ran
    assert dev.get("Temperature") == pytest.approx(0x194 * 0.0625)             # 0x1940 >> 4
    dev.bus.regs.update({0x00: 0xFF, 0x01: 0x00})
    assert dev.get("Temperature") == pytest.approx(-1 * 16 * 0.0625)           # negative, 12-bit two's complement
    dev.set("Output", 1.25)
    assert dev.bus.regs[0x40] == 0x04 and dev.bus.regs[0x41] == 0xE2 and dev.get("Output") == pytest.approx(1.25)
    with pytest.raises(LabratError):
        dev.set("Output", 4)
    assert dev.get("Mode") == 0b10 and dev.get("Alert") is True
    dev.set("Mode", 1)
    assert dev.bus.regs[0x10] == 0b1001_0000                                  # the other bits kept
    with pytest.raises(LabratError) as e:
        dev.set("Mode", 4)
    assert "does not fit" in e.value.message
    dev.set("Port", 0x0F)
    assert dev.get("Port") == 0x0F
    assert dev.call("ReadBytes", {"register": 0x40, "length": 2}) == [0x04, 0xE2]
    assert "WriteBytes" not in dev.functions
    with pytest.raises(LabratError):
        dev.set("Temperature", 1)                                             # read-only by default


# -- mqtt -------------------------------------------------------------------------------------------------------
class FakeMqttClient:
    instances = []

    def __init__(self, api, client_id=""):
        self.published, self.subscribed = [], []
        FakeMqttClient.instances.append(self)

    def username_pw_set(self, u, p):
        self.auth = (u, p)

    def reconnect_delay_set(self, a, b):
        pass

    def connect_async(self, host, port):
        self.target = (host, port)

    def loop_start(self):
        self.on_connect(self, None, None, 0)

    def loop_stop(self):
        pass

    def disconnect(self):
        pass

    def subscribe(self, t):
        self.subscribed.append(t)

    def publish(self, topic, payload, qos=0, retain=False):
        self.published.append((topic, payload))
        return types.SimpleNamespace(rc=0, wait_for_publish=lambda t: None)

    def deliver(self, topic, payload):
        self.on_message(self, None, types.SimpleNamespace(topic=topic, payload=payload.encode()))


@pytest.fixture
def paho(monkeypatch):
    client = types.ModuleType("paho.mqtt.client")
    client.Client = FakeMqttClient
    client.CallbackAPIVersion = types.SimpleNamespace(VERSION2=2)
    mqtt = types.ModuleType("paho.mqtt")
    mqtt.client = client
    pkg = types.ModuleType("paho")
    pkg.mqtt = mqtt
    monkeypatch.setitem(sys.modules, "paho", pkg)
    monkeypatch.setitem(sys.modules, "paho.mqtt", mqtt)
    monkeypatch.setitem(sys.modules, "paho.mqtt.client", client)
    monkeypatch.setenv("LAB_MQTT_PASSWORD", "pw")


def test_mqtt_light_reports_on_a_topic_and_takes_json_commands(paho):
    dev = build("lamp", {"driver": "mqtt", "kind": "lamp", "broker": "mqtt://192.0.2.10", "username": "lab", "password_env": "LAB_MQTT_PASSWORD",
                         "timeout": 0.2, "safe": {"Power": False},
                         "attributes": {
                             "Power": {"type": "bool", "state": "z2m/lamp", "json": "state", "command": "z2m/lamp/set",
                                       "payload": '{"state": "{value}"}', "on": "ON", "off": "OFF"},
                             "Brightness": {"type": "int", "min": 0, "max": 254, "state": "z2m/lamp", "json": "brightness",
                                            "command": "z2m/lamp/set", "payload": '{"brightness": {value}}'},
                             "Scene": {"type": "str", "command": "z2m/lamp/scene"}},
                         "functions": {"Blink": {"topic": "z2m/lamp/set", "payload": '{"effect": "blink"}'}}})
    c = FakeMqttClient.instances[-1]
    assert c.subscribed == ["z2m/lamp"] and c.auth == ("lab", "pw") and c.target == ("192.0.2.10", 1883)
    assert dev.kind == "light"
    with pytest.raises(LabratError) as e:
        dev.get("Power")
    assert e.value.code == "unavailable"
    c.deliver("z2m/lamp", '{"state": "ON", "brightness": 120}')
    assert dev.get("Power") is True and dev.get("Brightness") == 120
    dev.set("Brightness", 200)
    dev.set("Power", False)
    dev.call("Blink")
    assert c.published == [("z2m/lamp/set", '{"brightness": 200}'), ("z2m/lamp/set", '{"state": "OFF"}'),
                           ("z2m/lamp/set", '{"effect": "blink"}')]
    with pytest.raises(LabratError):
        dev.set("Brightness", 300)
    dev.set("Scene", "focus")
    assert dev.get("Scene") == "focus"                                        # no state topic: the value last written
    dev.make_safe()
    assert c.published[-1] == ("z2m/lamp/set", '{"state": "OFF"}')


# -- http -------------------------------------------------------------------------------------------------------
@pytest.fixture
def web(monkeypatch):
    state = {"on": False, "bri": 128}
    calls = []

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _reply(self, body, ctype="application/json"):
            data = body.encode()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            calls.append(("GET", self.path, None, self.headers.get("Authorization")))
            if self.path == "/json/state":
                return self._reply(json.dumps(state))
            if self.path == "/temp":
                return self._reply("T=21.5C", "text/plain")
            self.send_error(404)

        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"])).decode()
            calls.append(("POST", self.path, body, self.headers.get("Authorization")))
            state.update(json.loads(body))
            self._reply(json.dumps({"success": True, **state}))
    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setenv("STRIP_TOKEN", "t0k")
    yield f"http://127.0.0.1:{srv.server_address[1]}", state, calls
    srv.shutdown()


def test_http_device_reads_json_and_text_and_posts_templates(web):
    url, state, calls = web
    dev = build("strip", {"driver": "http", "kind": "light", "url": url, "headers": {"Authorization": "Bearer ${STRIP_TOKEN}"},
                          "attributes": {
                              "On": {"type": "bool", "get": "/json/state", "json": "on", "set": "POST /json/state", "body": '{"on": {value}}'},
                              "Brightness": {"type": "int", "min": 0, "max": 255, "get": "/json/state", "json": "bri",
                                             "set": "POST /json/state", "body": '{"bri": {value}}'},
                              "Temperature": {"get": "/temp", "reply": "T=([0-9.]+)", "unit": "°C"},
                              "Missing": {"get": "/nothing"}},
                          "functions": {"Preset": {"request": "POST /json/state", "body": '{"ps": {id}}', "args": {"id": "int"}}}})
    assert dev.get("On") is False and dev.get("Brightness") == 128 and dev.get("Temperature") == 21.5
    dev.set("On", True)
    dev.set("Brightness", 40)
    assert state == {"on": True, "bri": 40}
    assert dev.call("Preset", {"id": 3})["ps"] == 3
    assert calls[-1][2] == '{"ps": 3}' and all(c[3] == "Bearer t0k" for c in calls)
    with pytest.raises(LabratError) as e:
        dev.get("Missing")
    assert e.value.code == "device" and "404" in e.value.message
    with pytest.raises(LabratError):
        dev.set("Brightness", 256)


def test_http_and_mqtt_refuse_bad_settings(paho, monkeypatch):
    with pytest.raises(LabratError):
        build("x", {"driver": "http", "url": "ftp://a"})
    with pytest.raises(LabratError):
        build("x", {"driver": "http", "url": "http://a", "attributes": {"A": {"set": "SEND /x"}}})
    with pytest.raises(LabratError):
        build("x", {"driver": "http", "url": "http://a", "headers": {"A": "${NOT_SET_ANYWHERE}"}})
    with pytest.raises(LabratError):
        build("x", {"driver": "mqtt", "broker": "http://a"})
    with pytest.raises(LabratError):
        build("x", {"driver": "mqtt", "broker": "mqtt://a", "attributes": {"A": {"type": "int"}}})


# -- nut --------------------------------------------------------------------------------------------------------
def test_ups_through_nut(tmp_path):
    vars_ = {"battery.charge": "87", "battery.runtime": "1260", "ups.load": "23", "ups.status": "OB DISCHRG",
             "device.mfr": "Eaton", "device.model": "5E 1500", "device.serial": "G1234"}
    sent = []

    def answer(line):
        sent.append(line)
        parts = line.split()
        if parts[:2] == ["GET", "VAR"]:
            return f'VAR lab {parts[3]} "{vars_[parts[3]]}"\n' if parts[3] in vars_ else "ERR VAR-NOT-SUPPORTED\n"
        if parts[:2] == ["LIST", "VAR"]:
            return "BEGIN LIST VAR lab\n" + "".join(f'VAR lab {k} "{v}"\n' for k, v in vars_.items()) + "END LIST VAR lab\n"
        if parts[0] in ("USERNAME", "PASSWORD", "INSTCMD"):
            return "OK\n"
        return None
    srv, port, _ = line_server(answer)
    dev = build("ups", {"driver": "nut", "port": port, "ups": "lab", "commands": ["beeper.mute"], "username": "u", "password": "p",
                        "variables": {"Temperature": "ups.temperature"}})
    try:
        assert dev.kind == "ups"
        assert dev.get("BatteryCharge") == 87.0 and dev.get("BatteryRuntime") == 1260.0
        assert dev.get("OnBattery") is True and dev.get("LowBattery") is False and dev.get("Status") == "OB DISCHRG"
        with pytest.raises(LabratError) as e:
            dev.get("Temperature")
        assert "VAR-NOT-SUPPORTED" in e.value.message
        assert dev.call("Variables")["device.model"] == "5E 1500"
        assert dev.identity()["ref"] == "eaton/5e-1500#G1234"
        dev.call("Command", {"name": "beeper.mute"})
        assert [s for s in sent if s != "LOGOUT"][-3:] == ["USERNAME u", "PASSWORD p", "INSTCMD lab beeper.mute"]
        with pytest.raises(LabratError) as e:
            dev.call("Command", {"name": "shutdown.return"})
        assert e.value.code == "forbidden"
    finally:
        srv.close()


def test_drivers_say_what_to_install(monkeypatch):
    monkeypatch.setitem(sys.modules, "smbus2", None)
    monkeypatch.setitem(sys.modules, "paho.mqtt.client", None)
    with pytest.raises(LabratError) as e:
        build("x", {"driver": "i2c", "address": 0x48})
    assert "labrat-lab[i2c]" in e.value.message
    with pytest.raises(LabratError) as e:
        build("x", {"driver": "mqtt", "broker": "mqtt://a"})
    assert "labrat-lab[mqtt]" in e.value.message
