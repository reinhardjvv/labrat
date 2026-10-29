import json
import os
import time
import urllib.request

import pytest
from conftest import ADMIN, OPERATOR, OTHER, SIM, TOKENS, VIEWER, Http, load

from labrat import mcp
from labrat.client import Client
from labrat.http import Server
from labrat.hub import token_hash
from labrat.nest import Nest
from labrat.store import Store

AGENT = "agent-token"
AGENT_TOKEN = f'''
[[tokens]]
name = "claude-olu"
role = "operator"
for = "olu"
sha256 = "{token_hash(AGENT)}"
'''


def wait_until(check, seconds=5):
    deadline = time.monotonic() + seconds
    while not check() and time.monotonic() < deadline:
        time.sleep(0.05)
    return check()


def make(tmp_path, extra=""):
    store = tmp_path / "data"
    text = TOKENS + AGENT_TOKEN + f'''
[stores.lab]
path = "{store}"
[experiments]
store = "lab"
[recordings.supply]
store = "lab"
device = "psu"
read = ["VoltageLevel"]
every_s = 0.05
count = 3
''' + extra + SIM
    hub = Nest(load(tmp_path, "nest", text), browse=False)
    server = Server(hub, "127.0.0.1", 0).start()
    return hub, server, Http(f"http://127.0.0.1:{server.port}"), store


@pytest.fixture
def lab(tmp_path):
    hub, server, http_, store = make(tmp_path)
    yield hub, http_, store, f"http://127.0.0.1:{server.port}"
    server.close()
    hub.close()


def exp(http_, token, **args):
    name = args.pop("name", None)
    return http_.post({"op": "experiment", **({"name": name} if name else {}), "args": args}, token)


def test_an_experiment_keeps_its_commands_settings_and_recordings_in_the_persons_folder(lab):
    hub, http_, store, _ = lab
    s, r = exp(http_, OPERATOR, action="start", name="sweep", description="supply from 1 to 3 V")
    assert r["ok"], r
    folder = store / r["value"]["folder"]
    assert r["value"]["folder"] == f"olu/{time.strftime('%Y-%m-%d')}_sweep"
    for v in (1, 2, 3):
        http_.post({"op": "set", "device": "psu", "name": "VoltageLevel", "channel": "1", "value": v}, OPERATOR)
    http_.post({"op": "set", "device": "psu", "name": "VoltageLevel", "channel": "1", "value": 9}, OTHER)   # someone else
    assert http_.post({"op": "record", "name": "supply", "args": {"action": "start"}}, OPERATOR)[1]["ok"]
    assert wait_until(lambda: not hub.recorder.runs["supply"].running)
    assert exp(http_, OPERATOR, action="note", text="3 V looks stable")[1]["ok"]
    done = exp(http_, OPERATOR, action="stop")[1]["value"]
    assert done["state"] == "ended" and done["stopped_by"] == "olu" and done["notes"][0]["text"] == "3 V looks stable"

    log = [json.loads(x) for x in (folder / "log.jsonl").read_text().splitlines()]
    assert [e["value"] for e in log if e["op"] == "set"] == [1, 2, 3]           # olu's commands only
    assert log[-1]["args"] == {"action": "stop"}
    inst = json.loads((folder / "instruments.json").read_text())
    assert inst["start"]["devices"]["psu"]["identity"]["kind"] == "dc-power"
    assert inst["end"]["devices"]["psu"]["readings"]["VoltageLevel[1]"] == 9.0
    recs = list(folder.glob("supply-*.csv"))
    assert len(recs) == 1 and len(recs[0].read_text().splitlines()) == 4
    assert json.loads(next(folder.glob("supply-*.meta.json")).read_text())["experiment"] == "sweep"
    info = json.loads((folder / "experiment.json").read_text())
    assert info["description"] == "supply from 1 to 3 V" and info["recordings"][0]["readings"] == 3


def test_one_open_experiment_per_person_and_names_never_overwrite(lab):
    hub, http_, store, _ = lab
    assert exp(http_, OPERATOR, action="start", name="a")[1]["ok"]
    assert exp(http_, OPERATOR, action="start", name="b")[0] == 423
    assert exp(http_, OTHER, action="start", name="a")[1]["ok"]               # another person may
    exp(http_, OPERATOR, action="stop")
    again = exp(http_, OPERATOR, action="start", name="a")[1]["value"]
    assert again["folder"].endswith("_a-2")
    assert exp(http_, VIEWER, action="start", name="v")[0] == 403
    listing = exp(http_, OPERATOR, action="list")[1]["value"]
    assert {e["folder"].split("/")[1][11:] for e in listing} == {"a", "a-2"} and all(e["person"] == "olu" for e in listing)


def test_a_token_for_a_person_works_in_their_folder_and_cannot_approve(lab):
    hub, http_, store, _ = lab
    r = exp(http_, AGENT, action="start", name="agent-run")[1]["value"]
    assert r["folder"].startswith("olu/") and r["started_by"] == "claude-olu"
    http_.post({"op": "set", "device": "psu", "name": "VoltageLevel", "channel": "1", "value": 2}, AGENT)
    status = exp(http_, OPERATOR, action="status")[1]["value"]                # olu sees the agent's experiment
    assert status["experiment"] == "agent-run"
    log = (store / r["folder"] / "log.jsonl").read_text()
    assert '"who": "claude-olu"' in log and '"for": "olu"' in log
    hub.pending["a9"] = {"id": "a9", "by": "oth", "request": {}, "state": "pending", "asked": "", "_t": time.monotonic()}
    assert http_.post({"op": "approve", "name": "a9"}, AGENT)[0] == 403


def test_people_read_only_their_own_files(lab):
    hub, http_, store, url = lab
    r = exp(http_, OPERATOR, action="start", name="mine")[1]["value"]
    exp(http_, OPERATOR, action="note", text="ü" * 10)
    name = r["folder"].split("/")[1]
    top = http_.post({"op": "files"}, OPERATOR)[1]["value"]
    assert [f["name"] for f in top["files"]] == [name]
    inside = http_.post({"op": "files", "name": name}, OPERATOR)[1]["value"]
    assert {"experiment.json", "instruments.json"} <= {f["name"] for f in inside["files"]}
    part = http_.post({"op": "files", "name": f"{name}/experiment.json", "args": {"length": 40}}, OPERATOR)[1]["value"]
    assert part["more"] and part["text"].startswith("{")
    assert http_.post({"op": "files", "name": "../oth"}, OPERATOR)[0] == 400
    assert http_.post({"op": "files", "args": {"person": "oth"}}, OPERATOR)[0] == 403
    os.symlink(store, store / "olu" / "escape")
    assert http_.post({"op": "files", "name": "escape"}, OPERATOR)[0] == 403
    assert http_.post({"op": "files", "name": name, "args": {"person": "olu"}}, ADMIN)[1]["ok"]
    assert http_.post({"op": "files"}, VIEWER)[0] == 404                       # vic has none yet

    client = Client(url, OPERATOR)
    assert client.experiment()["experiment"] == "mine"
    dest = client.download(f"{name}/experiment.json", store.parent / "copy.json")
    assert json.loads(open(dest).read())["experiment"] == "mine"
    req = urllib.request.Request(f"{url}/api/v1/files/{name}/experiment.json?person=olu", headers={"Authorization": f"Bearer {OTHER}"})
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(req)
    assert e.value.code == 403


def test_open_experiments_carry_on_after_a_restart(tmp_path):
    hub, server, http_, store = make(tmp_path)
    folder = exp(http_, OPERATOR, action="start", name="long")[1]["value"]["folder"]
    server.close()
    hub.shutdown()
    hub.close()
    hub, server, http_, store = make(tmp_path)
    try:
        assert exp(http_, OPERATOR, action="status")[1]["value"]["folder"] == folder
        http_.post({"op": "set", "device": "psu", "name": "VoltageLevel", "channel": "1", "value": 1}, OPERATOR)
        assert '"value": 1' in (store / folder / "log.jsonl").read_text()
    finally:
        server.close()
        hub.close()


def test_experiments_are_optional(nest):
    hub, http_ = nest
    s, r = http_.post({"op": "experiment", "args": {"action": "list"}}, OPERATOR)
    assert s == 404 and "[experiments]" in r["error"]["message"]


def test_the_agent_tools_start_note_and_read(lab):
    hub, http_, store, url = lab
    client = Client(url, AGENT)
    r = mcp.call_tool(client, "labrat_experiment", {"action": "start", "name": "mcp-run", "description": "via MCP"})
    assert r["folder"].startswith("olu/")
    mcp.call_tool(client, "labrat_experiment", {"action": "note", "text": "first look"})
    folders = mcp.call_tool(client, "labrat_files", {})["files"]
    text = mcp.call_tool(client, "labrat_files", {"path": f"{folders[0]['name']}/experiment.json"})["text"]
    assert "first look" in text
    assert mcp.call_tool(client, "labrat_experiment", {"action": "stop"})["state"] == "ended"


# -- stores -----------------------------------------------------------------------------------------------------
def test_a_store_without_its_mount_refuses_files(tmp_path):
    s = Store("nas", {"path": str(tmp_path / "nas" / "labrat"), "mount": str(tmp_path / "nas")})
    assert not s.available()
    with pytest.raises(Exception) as e:
        s.root()
    assert "not mounted" in str(e.value)


def test_a_buffer_holds_files_until_the_store_is_back(tmp_path, monkeypatch):
    mounted = {"yes": False}
    monkeypatch.setattr(os.path, "ismount", lambda p: mounted["yes"])
    s = Store("nas", {"path": str(tmp_path / "nas"), "mount": str(tmp_path / "nas"), "buffer": str(tmp_path / "buf"), "sync_s": 3600})
    f = s.root() / "olu" / "run.csv"
    f.parent.mkdir(parents=True)
    f.write_text("a,b\n1,2\n")
    growing = s.root() / "olu" / "log.jsonl"
    growing.write_text("{}\n")
    s.opened(growing)
    assert s.sync() == 0 and s.waiting() == 2 and "not mounted" in s.status()["error"]
    mounted["yes"] = True
    assert s.sync() == 2
    assert (tmp_path / "nas" / "olu" / "run.csv").read_text() == "a,b\n1,2\n"
    assert not f.exists() and growing.exists()                                # a file still being written stays
    growing.write_text("{}\n{}\n")
    s.closed(growing)
    s.sync()
    assert (tmp_path / "nas" / "olu" / "log.jsonl").read_text() == "{}\n{}\n" and s.waiting() == 0
    assert [x["name"] for x in s.listing(tmp_path.joinpath("olu").relative_to(tmp_path))] == ["log.jsonl", "run.csv"]
    s.close()
