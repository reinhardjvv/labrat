import json

from conftest import ADMIN, OPERATOR, OTHER, VIEWER


def test_health_is_public_and_everything_else_needs_a_token(nest):
    _, http = nest
    status, body = http.get("/api/v1/health", token=None)
    assert status == 200 and body["value"]["role"] == "nest"
    assert http.get("/api/v1/devices", token=None)[0] == 401
    assert http.get("/api/v1/devices", token="wrong")[0] == 401
    assert http.post({"op": "list"}, token="wrong")[0] == 401


def test_set_get_and_the_limit(nest):
    _, http = nest
    assert http.post({"op": "set", "device": "psu", "name": "VoltageLevel", "channel": "1", "value": 5}, OPERATOR)[1]["ok"]
    status, r = http.post({"op": "get", "device": "psu", "name": "VoltageLevel", "channel": "1"}, VIEWER)
    assert r == {"ok": True, "value": 5.0, "unit": "V"}
    status, r = http.post({"op": "set", "device": "psu", "name": "VoltageLevel", "channel": "1", "value": 13}, OPERATOR)
    assert status == 422 and r["error"]["code"] == "limit"
    status, r = http.post({"op": "set", "device": "psu", "name": "VoltageLevel", "channel": "1", "value": 1}, VIEWER)
    assert status == 403


def test_describe_lists_limits_streams_and_safe_state(nest):
    _, http = nest
    status, r = http.get("/api/v1/devices/psu")
    d = r["value"]
    assert d["attributes"]["VoltageLevel"]["max"] == 12
    assert d["safe"] == {"OutputEnabled": False}
    assert "Readings" in d["streams"]


def test_a_lock_keeps_others_out_until_released(nest):
    _, http = nest
    assert http.post({"op": "lock", "device": "relays"}, OPERATOR)[1]["ok"]
    status, r = http.post({"op": "set", "device": "relays", "name": "heater", "value": True}, OTHER)
    assert status == 423 and "olu" in r["error"]["message"]
    assert http.post({"op": "set", "device": "relays", "name": "heater", "value": True}, OPERATOR)[1]["ok"]
    assert http.post({"op": "unlock", "device": "relays"}, OTHER)[0] == 423
    assert http.post({"op": "unlock", "device": "relays"}, ADMIN)[1]["ok"]          # an admin can free it
    assert http.post({"op": "set", "device": "relays", "name": "heater", "value": False}, OTHER)[1]["ok"]


def test_stop_makes_everything_safe_and_is_audited(nest):
    hub, http = nest
    http.post({"op": "set", "device": "relays", "name": "heater", "value": True}, OPERATOR)
    http.post({"op": "set", "device": "psu", "name": "OutputEnabled", "channel": "2", "value": True}, OPERATOR)
    status, r = http.post({"op": "stop", "args": {"reason": "test"}}, VIEWER)
    assert r["value"]["safe"] is True
    assert hub.devices["relays"].get("heater") is False and hub.devices["psu"].get("OutputEnabled", "2") is False
    log = [json.loads(line) for line in hub.audit_path.read_text().splitlines()]
    assert [e["op"] for e in log] == ["set", "set", "stop"] and log[0]["who"] == "olu"


def test_a_batch_is_answered_in_order(nest):
    _, http = nest
    status, r = http.post([{"op": "health", "id": 1}, {"op": "get", "device": "nope", "name": "x", "id": 2}])
    assert status == 200 and r[0]["ok"] and r[0]["id"] == 1 and r[1]["error"]["code"] == "not_found" and r[1]["id"] == 2


def test_a_stream_sends_live_readings(nest):
    import urllib.request
    _, http = nest
    req = urllib.request.Request(http.url + "/api/v1/streams/psu/Readings", headers={"Authorization": f"Bearer {VIEWER}"})
    with urllib.request.urlopen(req, timeout=5) as r:
        assert r.headers["Content-Type"] == "application/x-ndjson"
        rows = [json.loads(r.readline()) for _ in range(2)]
    assert rows[1]["time"] > rows[0]["time"] and rows[0]["1"]["V"] == 0
