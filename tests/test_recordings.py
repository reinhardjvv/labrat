import csv
import json
import time

import pytest
from conftest import OPERATOR, SIM, TOKENS, VIEWER, Http, load

from labrat.http import Server
from labrat.message import LabratError
from labrat.nest import Nest


def wait_until(check, seconds=5):
    deadline = time.monotonic() + seconds
    while not check() and time.monotonic() < deadline:
        time.sleep(0.05)
    return check()


@pytest.fixture
def lab(tmp_path):
    store = tmp_path / "data"
    rec = f'''
[stores.lab]
path = "{store}"
[recordings.supply]
store = "lab"
device = "psu"
read = ["VoltageLevel", "OutputEnabled"]
every_s = 0.05
count = 3
'''
    hub = Nest(load(tmp_path, "nest", TOKENS + rec + SIM))
    server = Server(hub, "127.0.0.1", 0).start()
    yield hub, Http(f"http://127.0.0.1:{server.port}"), store
    server.close()
    hub.close()


def test_readings_go_to_csv_with_times_and_a_description(lab):
    hub, http_, store = lab
    http_.post({"op": "set", "device": "psu", "name": "VoltageLevel", "channel": "1", "value": 4}, OPERATOR)
    status, r = http_.post({"op": "record", "name": "supply", "args": {"action": "start"}}, OPERATOR)
    assert r["ok"], r
    run = hub.recorder.runs["supply"]
    assert wait_until(lambda: not run.running)
    rows = list(csv.DictReader(open(run.path)))
    assert len(rows) == 3 and rows[0]["VoltageLevel[1]"] == "4.0" and rows[0]["OutputEnabled[2]"] == "False"
    assert rows[1]["unix_s"] > rows[0]["unix_s"]
    meta = json.loads(run.meta_path.read_text())
    assert meta["started_by"] == "olu" and meta["readings"] == 3 and "ended" in meta
    assert meta["description"]["attributes"]["VoltageLevel"]["unit"] == "V"
    assert run.path.parent.parent.name == "supply" and str(run.path).startswith(str(store))


def test_a_recording_can_be_made_on_the_spot_and_only_operators_start_one(lab):
    hub, http_, store = lab
    assert http_.post({"op": "record", "name": "m", "args": {"action": "start", "store": "lab", "device": "psu", "call": "Measure",
                                                            "args": {"type": "voltage"}, "channel": "1", "every_s": 0.05}}, VIEWER)[0] == 403
    status, r = http_.post({"op": "record", "name": "m", "args": {"action": "start", "store": "lab", "device": "psu", "call": "Measure",
                                                                 "args": {"type": "voltage"}, "channel": "1", "every_s": 0.05}}, OPERATOR)
    assert r["ok"], r
    time.sleep(0.3)
    stopped = http_.post({"op": "record", "name": "m", "args": {"action": "stop"}}, OPERATOR)[1]["value"]
    assert not stopped["running"] and stopped["readings"] >= 2
    line = json.loads(open(stopped["file"]).readline())
    assert line["value"]["unit"] == "V"
    listing = http_.post({"op": "record"}, VIEWER)[1]["value"]
    assert listing["stores"] == {"lab": {"path": str(store), "available": True}}


def test_a_stream_is_recorded_for_its_duration(lab):
    hub, http_, store = lab
    r = http_.post({"op": "record", "name": "live", "args": {"action": "start", "store": "lab", "device": "psu",
                                                             "stream": "Readings", "duration_s": 0.4}}, OPERATOR)[1]
    assert r["ok"], r
    run = hub.recorder.runs["live"]
    assert wait_until(lambda: not run.running)
    lines = run.path.read_text().splitlines()
    assert run.path.suffix == ".jsonl" and len(lines) >= 2 and "1" in json.loads(lines[0])


def test_mistakes_in_a_recording_are_named(tmp_path):
    for text, message in (('[recordings.x]\nstore = "nope"\ndevice = "psu"\nread = ["VoltageLevel"]\n', "store is one of"),
                          ('[stores.s]\npath = "/tmp"\n[recordings.x]\nstore = "s"\ndevice = "psu"\n', "exactly one of"),
                          ('[stores.s]\npath = "/tmp"\n[recordings.x]\nstore = "s"\ndevice = "psu"\nread = ["V"]\nformat = "xls"\n', "format")):
        with pytest.raises(LabratError, match=message):
            Nest(load(tmp_path, "nest", text + SIM))


def test_a_server_without_stores_says_so(nest):
    _, http_ = nest
    assert http_.post({"op": "record"})[0] == 404
