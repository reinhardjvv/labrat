import socket
import threading
import time

import pytest

from labrat.drivers import build
from labrat.message import LabratError


class FakeInstrument:
    """A line-based SCPI instrument on localhost that remembers what it was told."""

    def __init__(self):
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen()
        self.port = self.sock.getsockname()[1]
        self.received, self.volt = [], "0"
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            with conn, conn.makefile("rwb") as f:
                for line in f:
                    cmd = line.decode().strip()
                    self.received.append(cmd)
                    if cmd.startswith("VOLT "):
                        self.volt = cmd.split()[1]
                    elif cmd == "VOLT?":
                        f.write(f"{float(self.volt):.3E}\n".encode())
                        f.flush()
                    elif cmd == "CURV?":
                        data = bytes([0, 1, 2, 255, 128])
                        f.write(b"#15" + data + b"\n")
                        f.flush()
                    elif cmd == "WAVE?":
                        import struct
                        data = struct.pack(">3h", 1, -2, 300)
                        f.write(b"#16" + data + b"\n")
                        f.flush()
                    elif cmd == "TRACE?":
                        f.write(b"1.5,-2.25,3e3\n")
                        f.flush()
                    elif cmd == "*IDN?":
                        f.write(b"ACME,PSU-1,0,1.0\n")
                        f.flush()


@pytest.fixture
def psu():
    inst = FakeInstrument()
    dev = build("psu", {"driver": "scpi", "address": f"tcp://127.0.0.1:{inst.port}", "safe_commands": ["OUTP OFF"],
                        "attributes": {"VoltageLevel": {"type": "float", "unit": "V", "min": 0, "max": 12, "get": "VOLT?", "set": "VOLT {value}"},
                                       "OutputEnabled": {"type": "bool", "set": "OUTP {value}", "on": "ON", "off": "OFF"}},
                        "functions": {"Identify": {"query": "*IDN?"}}})
    yield inst, dev
    dev.close()
    inst.sock.close()


def test_attributes_and_functions_come_from_the_configuration(psu):
    inst, dev = psu
    dev.set("VoltageLevel", 3.3)
    assert dev.get("VoltageLevel") == pytest.approx(3.3)
    dev.set("OutputEnabled", True)
    assert dev.call("Identify").startswith("ACME")
    assert inst.received[:3] == ["VOLT 3.3", "VOLT?", "OUTP ON"]


def test_limits_hold_and_commands_cannot_be_smuggled(psu):
    inst, dev = psu
    with pytest.raises(LabratError, match="above the limit"):
        dev.set("VoltageLevel", 20)
    assert "Write" not in dev.functions and "Query" not in dev.functions            # raw is off by default
    assert not any("20" in c for c in inst.received)


def test_the_safe_commands_are_sent(psu):
    inst, dev = psu
    assert dev.make_safe() == []
    deadline = time.monotonic() + 2
    while "OUTP OFF" not in inst.received and time.monotonic() < deadline:   # a write is not answered
        time.sleep(0.01)
    assert inst.received[-1] == "OUTP OFF"


def test_raw_query_only_sends_queries():
    inst = FakeInstrument()
    dev = build("x", {"driver": "scpi", "address": f"tcp://127.0.0.1:{inst.port}", "raw": "query"})
    try:
        assert dev.call("Query", {"command": "*IDN?"}).startswith("ACME")
        with pytest.raises(LabratError):
            dev.call("Query", {"command": "VOLT 99"})
        with pytest.raises(LabratError):
            dev.call("Query", {"command": "*IDN?;VOLT 99"})
    finally:
        dev.close()
        inst.sock.close()


def test_an_unreachable_instrument_is_a_device_error():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    dev = build("x", {"driver": "scpi", "address": f"tcp://127.0.0.1:{port}", "functions": {"Identify": {"query": "*IDN?"}}})
    with pytest.raises(LabratError) as e:
        dev.call("Identify")
    assert e.value.code == "device"


def test_scopes_and_analysers_send_their_data():
    inst = FakeInstrument()
    dev = build("scope", {"driver": "scpi", "address": f"tcp://127.0.0.1:{inst.port}", "functions": {
        "Curve": {"query": "CURV?", "block": "int8"},
        "Wave": {"query": "WAVE?", "block": "int16", "byte_order": "big"},
        "Raw": {"query": "CURV?", "block": "bytes"},
        "Trace": {"query": "TRACE?", "parse": "floats"},
        "Identify": {"query": "*IDN?"}}})
    try:
        assert dev.call("Curve") == [0, 1, 2, -1, -128]
        assert dev.call("Wave") == [1, -2, 300]
        assert dev.call("Raw")["bytes"] == 5
        assert dev.call("Trace") == [1.5, -2.25, 3000.0]
        assert dev.call("Identify").startswith("ACME")              # the link is still in step after binary data
    finally:
        dev.close()
        inst.sock.close()
