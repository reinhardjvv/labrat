import json
import time
import urllib.request

import pytest
from conftest import OPERATOR, VIEWER, load

from labrat.http import Server
from labrat.message import LabratError
from labrat.rat import Rat


def adopt(nest, rat):
    rat_hub, rat_url = rat
    _, http = nest
    return http.post({"op": "adopt", "name": "bench-rat", "value": rat_hub.key, "args": {"url": rat_url}})


def test_a_rat_needs_its_key(nest, rat):
    _, http = nest
    status, r = http.post({"op": "adopt", "name": "bench-rat", "value": "wrong", "args": {"url": rat[1]}})
    assert not r["ok"] and r["error"]["code"] == "unauthorised"
    status, r = adopt(nest, rat)
    assert r["ok"] and r["value"]["fingerprint"] == rat[0].fingerprint
    assert http.post({"op": "adopt", "name": "x", "value": "k"}, OPERATOR)[0] == 403


def test_the_key_is_never_logged(nest, rat):
    adopt(nest, rat)
    assert rat[0].key not in nest[0].audit_path.read_text()


def test_rat_devices_appear_at_the_nest_with_both_limits(nest, rat, tmp_path):
    hub, http = nest
    adopt(nest, rat)
    hub.limits["bench-rat/psu"] = {"VoltageLevel": {"max": 3}}           # the nest is stricter than the rat
    ids = [d["device"] for d in http.post({"op": "list"})[1]["value"]]
    assert "bench-rat/psu" in ids and "bench-rat/relays" in ids
    assert http.post({"op": "set", "device": "bench-rat/psu", "name": "VoltageLevel", "channel": "1", "value": 2.5}, OPERATOR)[1]["ok"]
    assert rat[0].devices["psu"].get("VoltageLevel", "1") == 2.5
    status, r = http.post({"op": "set", "device": "bench-rat/psu", "name": "VoltageLevel", "channel": "1", "value": 4}, OPERATOR)
    assert status == 422 and "3 V" in r["error"]["message"]
    hub.limits.clear()
    status, r = http.post({"op": "set", "device": "bench-rat/psu", "name": "VoltageLevel", "channel": "1", "value": 20}, OPERATOR)
    assert status == 422 and "12 V" in r["error"]["message"]                # the rat's own limit still holds


def test_nest_locks_cover_rat_devices(nest, rat):
    _, http = nest
    adopt(nest, rat)
    assert http.post({"op": "lock", "device": "bench-rat/relays"}, OPERATOR)[1]["ok"]
    from conftest import OTHER
    assert http.post({"op": "set", "device": "bench-rat/relays", "name": "heater", "value": True}, OTHER)[0] == 423


def test_stop_at_the_nest_reaches_the_rats(nest, rat):
    _, http = nest
    adopt(nest, rat)
    http.post({"op": "set", "device": "bench-rat/relays", "name": "heater", "value": True}, OPERATOR)
    assert rat[0].devices["relays"].get("heater") is True
    assert http.post({"op": "stop"}, VIEWER)[1]["value"]["safe"]
    assert rat[0].devices["relays"].get("heater") is False


def test_a_stream_passes_through_the_nest(nest, rat):
    _, http = nest
    adopt(nest, rat)
    req = urllib.request.Request(http.url + "/api/v1/streams/bench-rat/psu/Readings", headers={"Authorization": f"Bearer {VIEWER}"})
    with urllib.request.urlopen(req, timeout=5) as r:
        assert "1" in json.loads(r.readline())


def test_adopted_rats_survive_a_nest_restart(nest, rat, tmp_path):
    from labrat.nest import Nest
    adopt(nest, rat)
    assert oct(nest[0].state_file.stat().st_mode & 0o777) == "0o600"
    again = Nest(load(tmp_path, "nest", ""))
    try:
        assert again.rats["bench-rat"].url == rat[1]
    finally:
        again.close()


def test_the_watchdog_makes_a_silent_rat_safe(tmp_path):
    hub = Rat(load(tmp_path, "rat", 'name = "dog"\nwatchdog_s = 1\n[devices.relays]\ndriver = "sim-gpio"\n'
                   'outputs = { heater = 17 }\nsafe = { heater = false }\n'))
    server = Server(hub, "127.0.0.1", 0).start()
    try:
        from labrat.client import Client
        Client(f"http://127.0.0.1:{server.port}", hub.key).set("relays", "heater", True)
        assert hub.devices["relays"].get("heater") is True
        deadline = time.monotonic() + 5
        while hub.devices["relays"].get("heater") and time.monotonic() < deadline:
            time.sleep(0.2)
        assert hub.devices["relays"].get("heater") is False
    finally:
        server.close()
        hub.close()


def test_a_rat_refuses_nest_operations(rat):
    from labrat.client import Client
    with pytest.raises(LabratError) as e:
        Client(rat[1], rat[0].key).rats()
    assert e.value.code in ("forbidden", "not_found")
