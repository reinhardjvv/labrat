import pytest
from conftest import OPERATOR, OTHER, SIM, TOKENS, Http, load

from labrat.http import Server
from labrat.hub import token_hash
from labrat.message import LabratError
from labrat.nest import Nest
from labrat.rat import Rat

GOD = "god-token"
GOD_TOKEN = f'''
[[tokens]]
name = "claude"
role = "operator"
god_mode = true
sha256 = "{token_hash(GOD)}"
'''
RULE = '''
[[approvals]]
who = ["*"]
ops = ["set"]
devices = ["relays"]
'''


@pytest.fixture
def lab(tmp_path):
    hub = Nest(load(tmp_path, "nest", TOKENS + GOD_TOKEN + RULE + SIM), browse=False)
    server = Server(hub, "127.0.0.1", 0).start()
    yield hub, Http(f"http://127.0.0.1:{server.port}")
    server.close()
    hub.close()


def setv(http_, token, v, device="psu"):
    return http_.post({"op": "set", "device": device, "name": "VoltageLevel", "channel": "1", "value": v}, token)


def test_god_mode_lifts_the_labs_limits_but_not_the_instruments_range(lab):
    hub, http_ = lab
    assert setv(http_, OPERATOR, 20)[0] == 422                                 # the lab's 12 V limit
    s, r = setv(http_, GOD, 20)
    assert r["ok"] and hub.devices["psu"].get("VoltageLevel", "1") == 20
    s, r = setv(http_, GOD, 1000)
    assert s == 422 and "range" in r["error"]["message"]                       # what the supply itself accepts
    assert http_.post({"op": "set", "device": "psu", "name": "Current", "channel": "1", "value": 1}, GOD)[0] in (403, 404)


def test_god_mode_skips_approvals_but_respects_locks(lab):
    hub, http_ = lab
    relay = {"op": "set", "device": "relays", "name": "heater", "value": True}
    assert http_.post(relay, OPERATOR)[0] == 202                               # waits for a person
    assert http_.post(relay, GOD)[1]["ok"]                                     # does not
    assert http_.post({"op": "lock", "device": "psu"}, OTHER)[1]["ok"]
    assert setv(http_, GOD, 5)[0] == 423                                       # someone else holds it
    assert http_.post({"op": "lock", "device": "psu"}, GOD)[0] == 423
    log = hub.audit_path.read_text()
    assert '"who": "claude", "role": "operator", "god": true' in log


def test_god_mode_is_granted_only_in_the_configuration(tmp_path):
    with pytest.raises(LabratError):
        load(tmp_path, "nest", TOKENS.replace('role = "viewer"', 'role = "viewer"\ngod_mode = true'))
        Nest(load(tmp_path, "nest", TOKENS.replace('role = "viewer"', 'role = "viewer"\ngod_mode = true')), browse=False)


def rat_with(tmp_path, allow):
    hub = Rat(load(tmp_path, "rat", f'name = "bench-rat"\nwatchdog_s = 0\nallow_god_mode = {str(allow).lower()}\n' + SIM))
    server = Server(hub, "127.0.0.1", 0).start()
    return hub, server, f"http://127.0.0.1:{server.port}"


@pytest.mark.parametrize("allow", [False, True])
def test_a_rat_keeps_its_own_limits_unless_it_allows_god_mode(lab, tmp_path, allow):
    hub, http_ = lab
    rat, server, url = rat_with(tmp_path, allow)
    try:
        assert http_.post({"op": "adopt", "name": "bench-rat", "value": rat.key, "args": {"url": url}})[1]["ok"]
        s, r = setv(http_, GOD, 20, "bench-rat/psu")
        assert r["ok"] is allow and (allow or "12 V" in r["error"]["message"])
        # an ordinary client cannot claim god mode on the way through the nest
        s, r = http_.post({"op": "set", "device": "bench-rat/psu", "name": "VoltageLevel", "channel": "1", "value": 20,
                           "args": {"god": True}}, OPERATOR)
        assert s == 422
        hub.limits.clear()
        s, r = http_.post({"op": "set", "device": "bench-rat/psu", "name": "VoltageLevel", "channel": "1", "value": 20,
                           "args": {"god": True}}, OPERATOR)
        assert s == 422 and "12 V" in r["error"]["message"]                    # stripped at the nest, refused at the rat
    finally:
        server.close()
        rat.close()
