"""Regression tests for problems found in review: key handling, locks, the audit log, limits and HTTP."""
import http.client
import json
import logging
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from conftest import OPERATOR, OTHER, VIEWER, load

from labrat.device import Attr
from labrat.message import LabratError
from labrat.nest import RatLink
from labrat.rat import Rat


class FakeRat:
    """A host that claims to be a rat: it answers health with any fingerprint and records what it is sent."""

    def __init__(self, health: dict, reply: dict | None = None):
        seen = self.seen = []

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _answer(self, body):
                data = json.dumps(body).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                seen.append(self.headers.get("Authorization"))
                self._answer({"ok": True, "value": health})

            def do_POST(self):
                seen.append(self.headers.get("Authorization"))
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                self._answer(reply or {"ok": True, "value": health})

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def test_a_fake_rat_never_receives_the_key(nest, rat):
    _, http_ = nest
    fake = FakeRat({"role": "rat", "name": "bench-rat", "fingerprint": rat[0].fingerprint, "proof": "0" * 64})
    try:
        status, r = http_.post({"op": "adopt", "name": "bench-rat", "value": rat[0].key, "args": {"url": fake.url}})
        assert r["error"]["code"] == "unauthorised"
        assert not any(a and rat[0].key in a for a in fake.seen)
        assert RatLink("bench-rat", rat[1], rat[0].key).verify(rat[1])
        assert not RatLink("bench-rat", fake.url, rat[0].key).verify(fake.url)
    finally:
        fake.close()


def test_a_key_never_reaches_the_log(nest, caplog):
    _, http_ = nest
    fake = FakeRat({"role": "rat"}, reply={"ok": True})
    try:
        with caplog.at_level(logging.DEBUG, logger="labrat"):
            http_.post({"op": "adopt", "name": "x", "value": "SECRETKEY2", "args": {"url": fake.url}})
            http_.post({"op": "adopt", "name": "x", "value": "SECRETKEY2", "args": {"url": 5}})
        assert "SECRETKEY2" not in caplog.text
    finally:
        fake.close()


def test_locks_survive_a_stop(nest):
    _, http_ = nest
    assert http_.post({"op": "lock", "device": "psu", "args": {"minutes": 2}}, OPERATOR)[1]["ok"]
    http_.post({"op": "stop"}, OTHER)
    assert http_.post({"op": "set", "device": "psu", "name": "VoltageLevel", "channel": "1", "value": 5}, OTHER)[0] == 423
    assert http_.post({"op": "lock", "device": "psu", "args": {"minutes": "abc"}}, OPERATOR)[0] == 400


def test_use_keeps_the_holder_s_lock_length(nest):
    hub, http_ = nest
    http_.post({"op": "lock", "device": "psu", "args": {"minutes": 120}}, OPERATOR)
    http_.post({"op": "set", "device": "psu", "name": "VoltageLevel", "channel": "1", "value": 1}, OPERATOR)
    assert hub.locks["psu"][1] - time.monotonic() > 100 * 60


def test_a_failed_audit_write_does_not_hide_the_change(nest, tmp_path):
    hub, http_ = nest
    hub.audit_path = tmp_path                               # a directory: every write fails
    status, r = http_.post({"op": "set", "device": "psu", "name": "VoltageLevel", "channel": "1", "value": 7}, OPERATOR)
    assert r["ok"] and hub.devices["psu"].get("VoltageLevel", "1") == 7


def test_call_arguments_are_audited_with_secrets_hidden(nest):
    hub, http_ = nest
    http_.post({"op": "call", "device": "psu", "name": "Measure", "channel": "1", "args": {"type": "current"}}, OPERATOR)
    entry = json.loads(hub.audit_path.read_text().splitlines()[-1])
    assert entry["args"] == {"type": "current"}
    hub.audit(hub.tokens[next(iter(hub.tokens))], {"op": "call", "args": {"password": "p"}}, True)
    assert "<hidden>" in hub.audit_path.read_text()


def test_limits_on_words_and_switches_are_refused():
    with pytest.raises(LabratError):
        Attr.from_dict("Volt", {"type": "str", "max": 5})
    with pytest.raises(LabratError):
        Attr("On", "bool").narrow(hi=1)


def test_the_nest_shows_its_own_limits_on_rat_devices(nest, rat):
    hub, http_ = nest
    http_.post({"op": "adopt", "name": "bench-rat", "value": rat[0].key, "args": {"url": rat[1]}})
    hub.limits["bench-rat/psu"] = {"VoltageLevel": {"max": 3}}
    d = http_.get("/api/v1/devices/bench-rat/psu")[1]["value"]
    assert d["attributes"]["VoltageLevel"]["max"] == 3


def test_a_nest_limit_for_a_missing_device_is_an_error(tmp_path):
    from labrat.nest import Nest
    with pytest.raises(LabratError, match="no such device"):
        Nest(load(tmp_path, "nest", '[limits.pus.VoltageLevel]\nmax = 1\n'))


def test_a_restarted_rat_goes_safe_without_requests(tmp_path):
    hub = Rat(load(tmp_path, "rat", 'name = "r"\nwatchdog_s = 1\n[devices.relays]\ndriver = "sim-gpio"\noutputs = { heater = 17 }\nsafe = { heater = false }\n'))
    try:
        assert hub.armed
        deadline = time.monotonic() + 5
        while hub.armed and time.monotonic() < deadline:
            time.sleep(0.1)
        assert not hub.armed
    finally:
        hub.close()


def test_an_error_closes_the_connection_and_bad_lengths_are_refused(nest):
    _, http_ = nest
    host, port = http_.url[7:].split(":")
    c = http.client.HTTPConnection(host, int(port), timeout=5)
    c.request("POST", "/api/v1/message", body=b'{"op": "list"}', headers={"Authorization": "Bearer wrong"})
    r = c.getresponse()
    assert r.status == 401 and r.getheader("Connection") == "close"
    r.read()
    c.close()
    c = http.client.HTTPConnection(host, int(port), timeout=5)
    c.putrequest("POST", "/api/v1/message")
    c.putheader("Authorization", f"Bearer {VIEWER}")
    c.putheader("Content-Length", "abc")
    c.endheaders()
    assert c.getresponse().status == 400
    c.close()


def test_approvals_hold_chosen_requests_for_a_person(tmp_path):
    from conftest import SIM, TOKENS, Http

    from labrat.http import Server
    from labrat.nest import Nest
    rules = '\n[[approvals]]\nwho = ["olu"]\nops = ["set"]\nnames = ["OutputEnabled"]\n'
    hub = Nest(load(tmp_path, "nest", TOKENS + rules + SIM))
    server = Server(hub, "127.0.0.1", 0).start()
    http_ = Http(f"http://127.0.0.1:{server.port}")
    try:
        assert http_.post({"op": "set", "device": "psu", "name": "VoltageLevel", "channel": "1", "value": 3}, OPERATOR)[1]["ok"]
        status, r = http_.post({"op": "set", "device": "psu", "name": "OutputEnabled", "channel": "1", "value": True}, OPERATOR)
        assert status == 202 and r["error"]["code"] == "pending"
        aid = r["error"]["approval"]
        assert hub.devices["psu"].get("OutputEnabled", "1") is False                  # nothing happened yet
        assert http_.post({"op": "approve", "name": aid}, OPERATOR)[0] == 403          # not by the one who asked
        assert http_.post({"op": "approve", "name": aid}, VIEWER)[0] == 403            # not by a viewer
        assert http_.post({"op": "approve", "name": aid}, OTHER)[1]["value"]["state"] == "approved"
        assert hub.devices["psu"].get("OutputEnabled", "1") is True
        assert http_.post({"op": "approval", "name": aid}, OPERATOR)[1]["value"]["decided_by"] == "oth"
        status, r = http_.post({"op": "set", "device": "psu", "name": "OutputEnabled", "channel": "2", "value": True}, OPERATOR)
        http_.post({"op": "stop"}, VIEWER)
        assert http_.post({"op": "approval", "name": r["error"]["approval"]}, VIEWER)[1]["value"]["state"] == "denied"
        assert hub.devices["psu"].get("OutputEnabled", "2") is False
        assert http_.post({"op": "set", "device": "psu", "name": "OutputEnabled", "channel": "2", "value": True}, OTHER)[1]["ok"]   # rule is for olu only
    finally:
        server.close()
        hub.close()


def test_mcp_summarises_long_data():
    from labrat import mcp
    s = mcp.summarise({"trace": list(range(1000))})
    assert s["trace"]["points"] == 1000 and s["trace"]["max"] == 999
