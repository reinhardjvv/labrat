import json
import os
import socket
import struct
import threading
import time
import tomllib
import tty
from pathlib import Path

import pytest
from conftest import ADMIN, OPERATOR, SIM, TOKENS, VIEWER, Http, load

from labrat import catalog, mcp
from labrat.capture import Capture, exchanges, parse_usbmon
from labrat.client import Client
from labrat.drivers import build
from labrat.drivers.frames import crc16_modbus
from labrat.http import Server
from labrat.hub import token_hash
from labrat.message import LabratError
from labrat.nest import Nest
from labrat.probe import Probe, is_query
from labrat.rat import Rat

MECER = Path(catalog.BUILTIN) / "mecer" / "me-2000-wpru.toml"
GOD = "god-token"


def fake_ups(tmp_path) -> str:
    """A serial device on a pseudo-terminal that answers like the recorded UPS, at any baud rate."""
    replies = {r["send"]: r["reply"] for r in tomllib.loads(MECER.read_text())["recording"]}
    m, s = os.openpty()
    tty.setraw(s)
    link = tmp_path / "ttyUPS"
    os.symlink(os.ttyname(s), link)

    def serve():
        buf = b""
        while True:
            try:
                buf += os.read(m, 64)
            except OSError:
                return
            while b"\r" in buf:
                line, buf = buf.split(b"\r", 1)
                r = replies.get(line.decode(errors="replace"))
                if r:
                    os.write(m, r.encode() + b"\r")
    threading.Thread(target=serve, daemon=True).start()
    return str(link)


@pytest.fixture
def lab(tmp_path):
    god = f'[[tokens]]\nname = "claude"\nrole = "operator"\ngod_mode = true\nsha256 = "{token_hash(GOD)}"\n'
    hub = Nest(load(tmp_path, "nest", TOKENS + god + SIM), browse=False)
    server = Server(hub, "127.0.0.1", 0).start()
    yield hub, Http(f"http://127.0.0.1:{server.port}"), tmp_path, f"http://127.0.0.1:{server.port}"
    server.close()
    hub.close()


# -- the catalog ------------------------------------------------------------------------------------------------
def test_the_built_in_profiles_load():
    cat = catalog.Catalog()
    assert cat.problems == []
    assert {"mecer/me-2000-wpru", "tektronix/afg3252", "tektronix/mso54"} <= set(cat.profiles)
    assert cat.by_usb("0665:5161")[0].id == "mecer/me-2000-wpru"
    assert cat.by_idn("TEKTRONIX,MSO54,C012345,CF:91.1CT FV:1.26.5.7750")[0].id == "tektronix/mso54"
    assert [r["profile"] for r in cat.listing("ups")] == ["mecer/me-2000-wpru"]
    assert [r["profile"] for r in cat.listing("tektronix/*")] == ["tektronix/afg3252", "tektronix/mso54"]


@pytest.mark.parametrize("pid", [p.id for p in catalog.Catalog().profiles.values() if p.recordings])
def test_every_recorded_profile_reads_every_attribute_from_its_recordings(pid):
    catalog.configure([])
    dev = build("x", {"profile": pid, "simulate": True})
    for name, a in dev.attrs.items():
        for ch in a.channels or [None]:
            dev.get(name, ch)                                                   # raises if a reply does not parse


def test_a_lab_overrides_a_profile_and_simulate_needs_one():
    catalog.configure([])
    dev = build("ups", {"profile": "mecer/me-2000-wpru", "simulate": True, "location": "rack 2",
                        "attributes": {"BatteryCharge": {"doc": "ours"}}})
    assert dev.identity() == {"kind": "ups", "manufacturer": "mecer", "model": "ME-2000-WPRU", "location": "rack 2"}
    assert dev.attrs["BatteryCharge"].doc == "ours" and dev.get("BatteryCharge") == 94.0 and dev.get("Mode") == "Line"
    assert dev.describe()["profile"] == "mecer/me-2000-wpru"
    with pytest.raises(LabratError):
        build("x", {"driver": "serial", "port": "/dev/null", "simulate": True})
    with pytest.raises(LabratError) as e:
        build("x", {"profile": "mecer/me-2000"})
    assert "did you mean mecer/me-2000-wpru" in e.value.message


def test_profiles_write_and_read_back_as_toml():
    data = tomllib.loads(MECER.read_text())
    again = tomllib.loads(catalog.dump(data))
    assert again == data


# -- probing ----------------------------------------------------------------------------------------------------
def test_probing_never_sends_words_that_reset_or_shut_down():
    for bad in ("S05", "*RST", "SYST:PRES", "reboot", "Q", "T", "FORMAT C", "update firmware"):
        assert not is_query(bad)
        with pytest.raises(LabratError) as e:
            Probe("/dev/null", sends=[bad])
        assert e.value.code == "forbidden"
    assert is_query("VOLT?") and is_query("QBV") and not is_query("VOLT 5")
    assert Probe("/dev/null", sends=["VOLT 5"]).needs_approval() == ["VOLT 5"]


def test_a_probe_finds_the_protocol_and_the_profile(lab):
    hub, http_, tmp, _ = lab
    port = fake_ups(tmp)
    s, r = http_.post({"op": "probe", "args": {"target": port, "baud": 2400}}, OPERATOR)
    t = r["value"]
    assert t["protocol"] == "megatec" and t["settings"] == {"line_ending": "cr", "baud": 2400}
    assert {"send": "QBV", "reply": "(054.6 04 01 094 306"}.items() <= next(x for x in t["exchanges"] if x["send"] == "QBV").items()
    assert t["profiles"] == [{"profile": "mecer/me-2000-wpru", "model": "ME-2000-WPRU"}]
    assert (tmp / "nest" / "probes" / t["file"]).is_file()
    assert http_.post({"op": "probe", "args": {"target": port}}, VIEWER)[0] == 403


def test_sending_anything_but_a_query_waits_for_a_person_even_in_god_mode(lab):
    hub, http_, tmp, _ = lab
    port = fake_ups(tmp)
    s, r = http_.post({"op": "probe", "args": {"target": port, "baud": 2400, "protocol": "megatec", "sends": ["PEA"]}}, GOD)
    assert s == 403                                                             # a beeper setting is on the deny list
    s, r = http_.post({"op": "probe", "args": {"target": port, "baud": 2400, "protocol": "megatec", "sends": ["QXYZ1"]}}, GOD)
    assert s == 202
    aid = r["error"]["approval"]
    s, r = http_.post({"op": "approve", "name": aid}, ADMIN)
    assert r["value"]["state"] == "approved" and any(x["send"] == "QXYZ1" for x in r["value"]["result"]["value"]["exchanges"])


# -- trying, saving and adding ----------------------------------------------------------------------------------
def test_from_a_draft_profile_to_a_device(lab):
    hub, http_, tmp, url = lab
    port = fake_ups(tmp)
    probe = http_.post({"op": "probe", "args": {"target": port, "baud": 2400, "protocol": "megatec"}}, OPERATOR)[1]["value"]
    draft = '''[profile]
manufacturer = "Acme Power"
model = "UPS-1"
kind = "ups"
status = "draft"

[device]
driver = "serial"
line_ending = "cr"
baud = 2400

[device.attributes.Charge]
get = "QBV"
reply = '^\\((?:\\S+ ){3}(\\S+)'
unit = "%"

[device.attributes.Nothing]
get = "QNOPE"
'''
    client = Client(url, OPERATOR)
    tried = client.try_profile(draft, port=port)
    assert tried["readings"] == {"Charge": 94.0} and "Nothing" in tried["problems"]
    assert {"send": "QBV", "reply": "(054.6 04 01 094 306"} in tried["exchanges"]
    with pytest.raises(LabratError) as e:
        client.save_profile(draft.replace('"draft"', '"tested"'))
    assert e.value.code == "forbidden"
    saved = client.save_profile(draft, transcripts=[probe["file"]])
    assert saved["profile"] == "acme-power/ups-1" and saved["recordings_added"] >= 5 and saved["status"] == "draft"
    assert "acme-power/ups-1" in [p["profile"] for p in client.profiles()]
    with pytest.raises(LabratError) as e:
        client.add_device("ups", profile="acme-power/ups-1", port=port)
    assert e.value.code == "forbidden"                                          # adding needs an admin
    admin = Client(url, ADMIN)
    assert admin.add_device("ups", profile="acme-power/ups-1", port=port)["profile"] == "acme-power/ups-1"
    assert admin.get("ups", "Charge") == 94.0
    with pytest.raises(LabratError) as e:
        admin.add_device("ups2", profile="acme-power/ups-1", port=port)
    assert e.value.code == "locked"                                             # the port is taken
    sim = admin.add_device("ups-sim", profile="acme-power/ups-1", simulate=True)
    assert sim["device"] == "ups-sim" and admin.get("ups-sim", "Charge") == 94.0
    with pytest.raises(LabratError):
        admin.remove_device("psu")                                              # in the file
    admin.remove_device("ups")
    assert "ups" not in hub.devices


def test_devices_added_while_running_come_back_after_a_restart(tmp_path):
    hub = Nest(load(tmp_path, "nest", TOKENS + SIM), browse=False)
    server = Server(hub, "127.0.0.1", 0).start()
    Client(f"http://127.0.0.1:{server.port}", ADMIN).add_device("ups", profile="mecer/me-2000-wpru", simulate=True)
    server.close()
    hub.close()
    hub = Nest(load(tmp_path, "nest", TOKENS + SIM + '[limits.ups.BatteryCharge]\nmax = 100\n'), browse=False)
    try:
        assert hub.devices["ups"].get("Mode") == "Line"
    finally:
        hub.close()


def test_the_nest_runs_workbench_operations_on_a_rat(tmp_path):
    rat = Rat(load(tmp_path, "rat", 'name = "bench-rat"\nwatchdog_s = 0\n' + SIM))
    rs = Server(rat, "127.0.0.1", 0).start()
    nest = Nest(load(tmp_path, "nest", TOKENS + SIM), browse=False)
    ns = Server(nest, "127.0.0.1", 0).start()
    try:
        admin = Client(f"http://127.0.0.1:{ns.port}", ADMIN)
        admin.adopt("bench-rat", rat.key, f"http://127.0.0.1:{rs.port}")
        assert isinstance(admin.scan(rat="bench-rat"), list)
        assert admin.add_device("bench-rat/ups", profile="mecer/me-2000-wpru", simulate=True)["device"] == "ups"
        assert "ups" in rat.devices and admin.get("bench-rat/ups", "BatteryVoltage") == 54.6
        with pytest.raises(LabratError) as e:
            Client(f"http://127.0.0.1:{ns.port}", OPERATOR).add_device("bench-rat/x", profile="mecer/me-2000-wpru", simulate=True)
        assert e.value.code == "forbidden"
        admin.remove_device("bench-rat/ups")
        assert "ups" not in rat.devices
    finally:
        ns.close()
        nest.close()
        rs.close()
        rat.close()


# -- binary frames ----------------------------------------------------------------------------------------------
def test_modbus_rtu_registers_and_coils():
    regs, coils = {0: 2305, 1: 0x4048, 2: 0xF5C3}, {5: 0}
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen()

    def serve():
        c, _ = srv.accept()
        while req := c.recv(64):
            unit, fn = req[0], req[1]
            a, v = struct.unpack(">HH", req[2:6])
            if fn == 3:
                data = b"".join(struct.pack(">H", regs[a + i]) for i in range(v))
                out = bytes([unit, 3, len(data)]) + data
            elif fn == 1:
                out = bytes([unit, 1, 1, coils[a]])
            elif fn in (5, 6):
                (coils if fn == 5 else regs)[a] = (1 if v else 0) if fn == 5 else v
                out = req[:6]
            else:
                out = bytes([unit, fn | 0x80, 1])
            c.sendall(out + crc16_modbus(out))
    threading.Thread(target=serve, daemon=True).start()
    d = build("m", {"driver": "frames", "protocol": "modbus-rtu", "port": f"socket://127.0.0.1:{srv.getsockname()[1]}",
                    "attributes": {"Voltage": {"register": 0, "scale": 0.1, "unit": "V", "access": "rw", "max": 300},
                                   "Power": {"register": 1, "type": "f32"}, "Relay": {"coil": 5, "access": "rw"},
                                   "Odd": {"register": 9, "input": True}}})
    try:
        assert d.get("Voltage") == pytest.approx(230.5) and d.get("Power") == pytest.approx(3.14, abs=1e-3) and d.get("Relay") is False
        d.set("Voltage", 230.0)
        d.set("Relay", True)
        assert d.get("Voltage") == 230.0 and d.get("Relay") is True
        with pytest.raises(LabratError):
            d.set("Voltage", 400)
        with pytest.raises(LabratError) as e:
            d.get("Odd")
        assert "exception" in e.value.message
    finally:
        d.close()
        srv.close()


def test_fixed_frames_with_a_checksum_from_recordings(tmp_path):
    folder = tmp_path / "cat" / "acme"
    folder.mkdir(parents=True)
    (folder / "load-1.toml").write_text('''[profile]
manufacturer = "acme"
model = "load-1"

[device]
driver = "frames"
checksum = "sum8"

[device.attributes.Current]
send = "AA 01 01 {checksum}"
reply_length = 6
check_reply = true
field = { offset = 3, type = "u16be" }
scale = 0.001
unit = "A"

[[recording]]
send_hex = "aa 01 01 ac"
reply_hex = "aa 81 01 04 d2 02"
''')
    catalog.configure([tmp_path / "cat"])
    d = build("load", {"profile": "acme/load-1", "simulate": True})
    assert d.get("Current") == pytest.approx(1.234)


# -- captures ---------------------------------------------------------------------------------------------------
def test_a_serial_capture_passes_bytes_through_and_keeps_them(tmp_path):
    import serial
    port = fake_ups(tmp_path)
    c = Capture("serial", port, tmp_path / "captures", 2400)
    app = serial.Serial(c.link, timeout=2)                                     # the vendor's software
    app.write(b"QBV\r")
    assert app.read_until(b"\r") == b"(054.6 04 01 094 306\r"
    time.sleep(0.2)
    app.close()
    r = c.stop()
    t = json.loads((tmp_path / "captures" / r["file"]).read_text())
    assert t["exchanges"][0]["send"] == "QBV" and t["exchanges"][0]["reply"] == "(054.6 04 01 094 306"
    assert catalog.recordings_from([t]) == [{"send": "QBV", "reply": "(054.6 04 01 094 306"}]


def test_usbmon_lines_become_exchanges():
    lines = ["ffff 1 S Io:1:004:2 -115:8 8 = 00514256 0d000000",
             "ffff 2 C Ii:1:004:1 0:8 8 = 28303534 2e362030",
             "ffff 3 C Ii:1:004:1 0:8 8 = 3420300d 00000000",
             "ffff 4 C Ii:1:007:1 0:8 8 = 41414141 41414141"]                       # another device
    events = []
    for i, line in enumerate(lines):
        e = parse_usbmon(line, 4)
        if e:
            events.append({"dir": e[0], "text": e[1].decode(), "t": i * 0.01})
    assert exchanges(events) == [{"send": "QBV", "t": 0.0, "reply": "(054.6 04 0"}]


# -- for agents -------------------------------------------------------------------------------------------------
def test_the_agent_tools_cover_the_workbench(lab):
    hub, http_, tmp, url = lab
    port = fake_ups(tmp)
    client = Client(url, OPERATOR)
    t = mcp.call_tool(client, "labrat_probe", {"target": port, "baud": 2400, "protocol": "megatec"})
    assert t["profiles"][0]["profile"] == "mecer/me-2000-wpru"
    shown = mcp.call_tool(client, "labrat_profiles", {"id": "mecer/me-2000-wpru"})
    assert "[[recording]]" in shown["toml"]
    tried = mcp.call_tool(client, "labrat_try_profile", {"profile": "mecer/me-2000-wpru", "device": {"port": port}})
    assert tried["verdict"] == "every attribute read"
    assert isinstance(mcp.call_tool(client, "labrat_scan", {}), list)
