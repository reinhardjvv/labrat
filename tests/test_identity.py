import pytest
from conftest import ADMIN, load

from labrat import ident
from labrat.client import Client
from labrat.drivers import build
from labrat.http import Server
from labrat.message import LabratError
from labrat.nest import Nest


def test_idn_replies_become_an_identity():
    i = ident.parse_idn("TEKTRONIX,MSO54,C012345,CF:91.1CT FV:1.40")
    assert i == {"manufacturer": "tektronix", "model": "MSO54", "serial": "C012345", "firmware": "CF:91.1CT FV:1.40"}
    assert ident.reference(i) == "tektronix/mso54#C012345"
    assert ident.parse_idn("Rohde&Schwarz,ZND,1234,1.0")["manufacturer"] == "rohde-schwarz"
    assert ident.maker("Keysight Technologies") == "keysight"


def test_kinds_are_standard_and_aliases_are_understood():
    assert ident.kind_of("Spectrum Analyzer") == "spectrum-analyser"
    assert ident.kind_of("scope") == "oscilloscope" and ident.kind_of("SLM") == "display"
    with pytest.raises(LabratError, match="did you mean"):
        ident.kind_of("oscilloscop")


def test_selectors_read_like_spack_specs():
    sel = ident.parse_selector("oscilloscope tektronix/mso5* #C01* location=optics*")
    assert sel == {"kind": "oscilloscope", "manufacturer": "tektronix", "model": "mso5*", "serial": "C01*", "location": "optics*"}
    e = {"device": "scope", "kind": "oscilloscope", "manufacturer": "tektronix", "model": "MSO54", "serial": "C012345",
         "location": "optics table"}
    assert ident.matches(e, sel)
    assert not ident.matches(e, ident.parse_selector("vna"))
    assert ident.matches(e, ident.parse_selector("sco*"))            # a word that is not a kind matches device ids


LAB = '''
[devices.scope]
driver = "sim-supply"
kind = "oscilloscope"
manufacturer = "Tektronix, Inc."
model = "MSO54"
serial = "C012345"
location = "optics table"
[devices.scope-2]
driver = "sim-supply"
kind = "scope"
manufacturer = "tektronix"
model = "MSO54"
serial = "C099999"
[devices.vna]
driver = "sim-supply"
kind = "vna"
manufacturer = "Rohde & Schwarz"
model = "ZND"
serial = "1"
[devices.vna-again]
driver = "sim-supply"
kind = "vna"
manufacturer = "rohde-schwarz"
model = "ZND"
serial = "1"
'''


@pytest.fixture
def lab(tmp_path):
    from conftest import TOKENS
    hub = Nest(load(tmp_path, "nest", TOKENS + LAB))
    server = Server(hub, "127.0.0.1", 0).start()
    yield hub, f"http://127.0.0.1:{server.port}"
    server.close()
    hub.close()


def test_two_identical_scopes_stay_apart_and_a_repeated_instrument_is_flagged(lab):
    _, url = lab
    c = Client(url, ADMIN)
    assert c.find("oscilloscope tektronix/mso54") == ["scope", "scope-2"]
    assert c.find("#C099999") == ["scope-2"]
    rows = {d["device"]: d for d in c.devices()}
    assert rows["scope"]["ref"] == "tektronix/mso54#C012345" and rows["scope"]["manufacturer"] == "tektronix"
    assert rows["vna-again"]["duplicate_of"] == "vna"
    assert [d["device"] for d in c.devices(sort="manufacturer")][:2] == ["vna", "vna-again"]


def test_a_device_handle_is_loaded_by_name(lab):
    _, url = lab
    c = Client(url, ADMIN)
    scope = c.device("oscilloscope location=optics*")
    assert scope.id == "scope"
    scope.set("VoltageLevel", 2, channel="1")
    assert c.get("scope", "VoltageLevel", "1") == 2
    with pytest.raises(LabratError, match="scope, scope-2"):
        c.device("oscilloscope")


def test_an_unknown_kind_in_the_configuration_is_an_error():
    with pytest.raises(LabratError, match="unknown kind"):
        build("x", {"driver": "sim-supply", "kind": "toaster"})


def test_scpi_instruments_say_who_they_are():
    from test_scpi import FakeInstrument
    inst = FakeInstrument()
    dev = build("psu", {"driver": "scpi", "address": f"tcp://127.0.0.1:{inst.port}", "kind": "psu", "location": "bench 2"})
    try:
        i = dev.identity()
        assert i["kind"] == "dc-power" and i["manufacturer"] == "acme" and i["model"] == "PSU-1" and i["location"] == "bench 2"
        assert "serial" not in i                                     # '0' means the instrument gave none
    finally:
        dev.close()
        inst.sock.close()


def test_the_cli_groups_devices(lab, capsys):
    from labrat import cli
    _, url = lab
    assert cli.main(["devices", "--by", "kind", "--url", url, "--token", ADMIN]) == 0
    out = capsys.readouterr().out
    assert "oscilloscope" in out and "vna" in out and "same instrument as vna" in out
