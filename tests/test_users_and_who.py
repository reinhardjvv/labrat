import json
import os

import pytest
from conftest import ADMIN, OPERATOR, OTHER, SIM, TOKENS, VIEWER, Http, load

from labrat.http import Server
from labrat.nest import Nest


def start(tmp_path, extra=""):
    hub = Nest(load(tmp_path, "nest", extra + TOKENS + SIM), browse=False)
    server = Server(hub, "127.0.0.1", 0).start()
    return hub, server, Http(f"http://127.0.0.1:{server.port}")


@pytest.fixture
def lab(tmp_path):
    hub, server, http_ = start(tmp_path)
    yield hub, http_, tmp_path
    server.close()
    hub.close()


def setv(http_, token, v=3):
    return http_.post({"op": "set", "device": "psu", "name": "VoltageLevel", "channel": "1", "value": v}, token)


def test_an_admin_adds_and_removes_users_while_the_nest_runs(lab):
    hub, http_, tmp = lab
    assert http_.post({"op": "users", "name": "eve", "args": {"action": "add"}}, OPERATOR)[0] == 403
    r = http_.post({"op": "users", "name": "eve", "args": {"action": "add", "role": "operator"}}, ADMIN)[1]["value"]
    token = r["token"]
    assert setv(http_, token)[1]["ok"]                                          # works at once
    stored = (tmp / "nest" / "users.json").read_text()
    assert token not in stored and oct(os.stat(tmp / "nest" / "users.json").st_mode & 0o777) == "0o600"
    assert token not in hub.audit_path.read_text()
    users = {u["name"]: u for u in http_.post({"op": "users"}, VIEWER)[1]["value"]}
    assert users["eve"]["added_by"] == "ada" and "last_active" in users["eve"] and users["olu"]["from"] == "nest.toml"
    assert http_.post({"op": "users", "name": "olu", "args": {"action": "add"}}, ADMIN)[0] == 400          # taken
    assert http_.post({"op": "users", "name": "olu", "args": {"action": "remove"}}, ADMIN)[0] == 400       # in the file
    assert http_.post({"op": "lock", "device": "psu"}, token)[1]["ok"]
    assert http_.post({"op": "users", "name": "eve", "args": {"action": "remove"}}, ADMIN)[1]["ok"]
    assert setv(http_, token)[0] == 401                                         # stops working at once
    assert hub.holder("psu") is None                                            # and her lock is released


def test_users_survive_a_restart_and_can_act_for_a_person(tmp_path):
    hub, server, http_ = start(tmp_path)
    token = http_.post({"op": "users", "name": "claude-olu", "args": {"action": "add", "for": "olu"}}, ADMIN)[1]["value"]["token"]
    server.close()
    hub.close()
    hub, server, http_ = start(tmp_path)
    try:
        assert setv(http_, token)[1]["ok"]
        assert json.loads(hub.audit_path.read_text().splitlines()[-1])["for"] == "olu"
    finally:
        server.close()
        hub.close()


def test_who_shows_holders_last_changes_and_activity(lab):
    hub, http_, _ = lab
    setv(http_, OPERATOR, 4)
    http_.post({"op": "lock", "device": "relays", "args": {"minutes": 5}}, OTHER)
    r = http_.post({"op": "who"}, VIEWER)[1]["value"]
    rows = {d["device"]: d for d in r["devices"]}
    assert rows["psu"]["last_change"]["who"] == "olu" and rows["psu"]["last_change"]["value"] == 4
    assert rows["relays"]["locked_by"] == "oth" and 4 < rows["relays"]["minutes_left"] <= 5
    assert {p["name"] for p in r["people"]} >= {"olu", "oth", "vic"}


def test_who_remembers_the_last_changes_after_a_restart(tmp_path):
    hub, server, http_ = start(tmp_path)
    setv(http_, OPERATOR, 6)
    server.close()
    hub.close()
    hub, server, http_ = start(tmp_path)
    try:
        rows = {d["device"]: d for d in http_.post({"op": "who"})[1]["value"]["devices"]}
        assert rows["psu"]["last_change"]["value"] == 6
    finally:
        server.close()
        hub.close()


def test_a_lab_can_require_a_lock_before_any_change(tmp_path):
    hub, server, http_ = start(tmp_path, 'require_lock = ["psu"]\n')
    try:
        s, r = setv(http_, OPERATOR)
        assert s == 423 and "lock psu before changing it" in r["error"]["message"]
        assert http_.post({"op": "set", "device": "relays", "name": "heater", "value": True}, OPERATOR)[1]["ok"]   # not listed
        assert http_.post({"op": "lock", "device": "psu"}, OPERATOR)[1]["ok"]
        assert setv(http_, OPERATOR)[1]["ok"]
        assert setv(http_, OTHER)[0] == 423                                     # held by olu
        assert http_.post({"op": "get", "device": "psu", "name": "VoltageLevel", "channel": "1"}, OTHER)[1]["ok"]   # reading is fine
        assert http_.post({"op": "who"})[1]["value"]["require_lock"] == ["psu"]
    finally:
        server.close()
        hub.close()
