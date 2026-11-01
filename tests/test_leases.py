import json
import time

import pytest
from conftest import ADMIN, OPERATOR, OTHER, SIM, TOKENS, load

from labrat import lease as leases
from labrat.client import Client
from labrat.http import Server
from labrat.hub import Principal, Rule
from labrat.message import LabratError, Message
from labrat.nest import Nest
from labrat.rat import Rat


@pytest.fixture
def lab(tmp_path):
    fb = tmp_path / "fb"
    fb.write_bytes(bytes(8 * 4 * 4))
    rat = Rat(load(tmp_path, "rat", 'name = "optics-rat"\nwatchdog_s = 0\n' + TOKENS + SIM + f'''
[devices.slm]
driver = "display"
framebuffer = "{fb}"
width = 8
height = 4
bits_per_pixel = 32
[devices.counted]
driver = "sim-supply"
log = "every"
'''))
    rat_server = Server(rat, "127.0.0.1", 0).start()
    nest = Nest(load(tmp_path, "nest", TOKENS + '\n[limits."optics-rat/psu".VoltageLevel]\nmax = 3\n'))
    nest_server = Server(nest, "127.0.0.1", 0).start()
    url = f"http://127.0.0.1:{nest_server.port}"
    olu = Client(url, OPERATOR)
    Client(url, ADMIN).adopt("optics-rat", rat.key, f"http://127.0.0.1:{rat_server.port}")
    yield nest, rat, olu, Client(url, OTHER), fb
    nest_server.close()
    rat_server.close()
    nest.close()
    rat.close()


def rat_audit(rat):
    return [json.loads(line) for line in rat.audit_path.read_text().splitlines()]


def test_a_direct_device_talks_to_the_rat_within_the_nests_limits(lab):
    nest, rat, olu, oth, fb = lab
    with olu.direct("optics-rat/psu") as psu:
        assert psu.path in nest.rats["optics-rat"].urls()
        assert psu.set("VoltageLevel", 2.5, channel="1") == 2.5
        assert rat.devices["psu"].get("VoltageLevel", "1") == 2.5
        with pytest.raises(LabratError) as e:
            psu.set("VoltageLevel", 5, channel="1")                # the rat allows 12; the nest's 3 travels in the lease
        assert e.value.code == "limit"
        with pytest.raises(LabratError) as e:
            oth.set("optics-rat/psu", "VoltageLevel", 1, channel="1")
        assert e.value.code == "locked"                            # the nest locked it for the holder
        with pytest.raises(LabratError) as e:
            Client(nest.rats["optics-rat"].url, ADMIN).set("psu", "VoltageLevel", 1, channel="1")      # the rat's own token
        assert e.value.code == "locked" and "leased to olu" in e.value.message
    assert nest.holder("optics-rat/psu") is None and not rat.leases
    assert rat.devices["psu"].get("VoltageLevel", "1") == 2.5      # releasing leaves the device as it is


def test_a_lease_is_for_its_devices_only(lab):
    nest, rat, olu, oth, fb = lab
    grant = olu.lease("optics-rat/psu")
    direct = Client(grant["urls"][0], grant["lease"], scheme="Lease")
    assert direct.get("psu", "VoltageLevel", channel="1") == 0
    for call in (lambda: direct.set("relays", "heater", True), lambda: direct.get("relays", "door"),
                 lambda: direct.request("adopt", name="x", value="y")):
        with pytest.raises(LabratError) as e:
            call()
        assert e.value.code == "forbidden"
    import urllib.request
    req = urllib.request.Request(grant["urls"][0] + "/api/v1/streams/relays/Live", headers={"Authorization": f"Lease {grant['lease']}"})
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(req, timeout=5)
    assert e.value.code == 403
    data, _, sig = grant["lease"].partition(".")
    forged = Client(grant["urls"][0], data + "." + "0" * 64, scheme="Lease")
    with pytest.raises(LabratError) as e:
        forged.get("psu", "VoltageLevel", channel="1")
    assert e.value.code == "unauthorised"
    olu.release_lease(grant["id"])
    with pytest.raises(LabratError) as e:                          # a released lease is not taken again
        direct.get("psu", "VoltageLevel", channel="1")
    assert e.value.code == "unauthorised"


def test_a_lease_that_runs_out_leaves_the_devices_safe(lab):
    nest, rat, olu, oth, fb = lab
    grant = olu.lease("optics-rat/relays", minutes=0.02)
    direct = Client(grant["urls"][0], grant["lease"], scheme="Lease")
    direct.set("relays", "heater", True)
    assert rat.devices["relays"].get("heater") is True
    deadline = time.time() + 10
    while rat.devices["relays"].get("heater") and time.time() < deadline:
        time.sleep(0.1)
    assert rat.devices["relays"].get("heater") is False
    end = [e for e in rat_audit(rat) if e.get("op") == "lease"][-1]
    assert end["args"]["action"] == "ran out" and end["args"]["safe_state"] is True


def test_a_renewed_lease_carries_on(lab):
    nest, rat, olu, oth, fb = lab
    grant = olu.lease("optics-rat/relays", minutes=0.05)
    direct = Client(grant["urls"][0], grant["lease"], scheme="Lease")
    direct.set("relays", "heater", True)
    time.sleep(2)
    direct.token = olu.renew_lease(grant["id"])["lease"]
    time.sleep(2)
    assert rat.devices["relays"].get("heater") is True and direct.get("relays", "heater") is True
    with pytest.raises(LabratError):
        oth.renew_lease(grant["id"])
    assert olu.release_lease(grant["id"])["rat"]["commands"] == 1


def test_no_lease_where_changes_wait_for_approval(lab):
    nest, rat, olu, oth, fb = lab
    nest.rules.append(Rule.from_dict({"who": ["olu"], "devices": ["optics-rat/relays"]}))
    with pytest.raises(LabratError) as e:
        olu.lease("optics-rat/relays")
    assert e.value.code == "forbidden" and "approval" in e.value.message
    with pytest.raises(LabratError):
        olu.lease("psu-on-the-nest")


def test_a_jobs_lease_covers_only_the_jobs_devices(lab):
    nest, rat, olu, oth, fb = lab
    job = Principal("olu", "operator", job=7, devices=("optics-rat/psu",))
    with pytest.raises(LabratError) as e:
        nest.lease_op(Message(op="lease", args={"devices": ["optics-rat/relays"]}), job)
    assert e.value.code == "forbidden"
    assert nest.lease_op(Message(op="lease", args={"devices": ["optics-rat/psu"]}), job)["devices"] == ["optics-rat/psu"]


def test_fast_changes_are_counted_in_the_log(lab):
    nest, rat, olu, oth, fb = lab
    with olu.direct("optics-rat/psu") as psu:
        for i in range(30):
            psu.set("VoltageLevel", i / 10, channel="1")
    sets = [e for e in rat_audit(rat) if e.get("op") == "set" and e.get("device") == "psu"]
    assert len(sets) == 2 and "value" in sets[0] and sets[1]["count"] == 29 and len(sets[1]["sha256"]) == 64
    with olu.direct("optics-rat/counted") as c:
        for i in range(5):
            c.set("VoltageLevel", i, channel="1")
    assert len([e for e in rat_audit(rat) if e.get("device") == "counted" and e.get("op") == "set"]) == 5


def test_frames_travel_as_raw_bytes(lab):
    nest, rat, olu, oth, fb = lab
    frame = bytes(range(32))
    with olu.direct("optics-rat/slm") as slm:
        assert slm.path != "nest"
        slm.call("ShowImage", data=frame)
    assert fb.read_bytes()[0::4] == frame
    olu.call("optics-rat/slm", "ShowImage", data=bytes(reversed(frame)))     # through the nest, on to the rat as base64
    assert fb.read_bytes()[0::4] == bytes(reversed(frame))
    entry = [e for e in rat_audit(rat) if e.get("name") == "ShowImage"][0]
    assert entry["args"]["data"]["bytes"] == 32                                # the log keeps its size and hash


def test_the_holder_checks_it_reached_the_real_rat(lab, monkeypatch):
    nest, rat, olu, oth, fb = lab
    grant = olu.lease("optics-rat/psu")
    olu.release_lease(grant["id"])
    real = Client.lease

    def elsewhere(self, *devices, minutes=None):
        g = real(self, *devices, minutes=minutes)
        g["urls"] = ["http://127.0.0.1:9", nest.rats["optics-rat"].url]
        g["secret"] = "not the rat's secret"                             # as if another computer answered
        return g
    monkeypatch.setattr(Client, "lease", elsewhere)
    psu = olu.direct("optics-rat/psu")
    assert psu.path == "nest" and psu.set("VoltageLevel", 1, channel="1") == 1
    assert nest.holder("optics-rat/psu") == "olu"
    psu.close()
    assert nest.holder("optics-rat/psu") is None


def test_the_secret_proves_the_rat(lab):
    nest, rat, olu, oth, fb = lab
    s = leases.secret(rat.key, "abc")
    assert rat.health("c1", "abc")["lease_proof"] == leases.proof(s, "c1")
    assert "addresses" not in rat.health()


def test_a_stop_ends_every_lease(lab):
    nest, rat, olu, oth, fb = lab
    grant = olu.lease("optics-rat/relays")
    direct = Client(grant["urls"][0], grant["lease"], scheme="Lease")
    direct.set("relays", "heater", True)
    olu.stop("test")
    assert rat.devices["relays"].get("heater") is False and not rat.leases
    with pytest.raises(LabratError):
        direct.set("relays", "heater", True)


def test_a_removed_persons_lease_is_revoked(lab):
    nest, rat, olu, oth, fb = lab
    admin = Client(oth.url, ADMIN)
    token = admin.add_user("temp", "operator")["token"]
    temp = Client(oth.url, token)
    grant = temp.lease("optics-rat/relays")
    Client(grant["urls"][0], grant["lease"], scheme="Lease").set("relays", "heater", True)
    admin.remove_user("temp")
    assert not rat.leases and rat.devices["relays"].get("heater") is False


def test_connections_stay_open(lab):
    nest, rat, olu, oth, fb = lab
    olu.devices()
    first = olu._local.conn
    olu.devices()
    assert olu._local.conn is first
