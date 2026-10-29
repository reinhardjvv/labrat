import io
import json

from conftest import OPERATOR

from labrat import cli, mcp
from labrat.client import Client


def rpc(client, *reqs):
    out = io.StringIO()
    mcp.run(client, io.StringIO("\n".join(json.dumps(r) for r in reqs) + "\n"), out)
    return [json.loads(line) for line in out.getvalue().splitlines()]


def test_mcp_tools_are_lab_messages(nest):
    hub, http = nest
    client = Client(http.url, OPERATOR)
    init, tools, setv, refused = rpc(
        client,
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "labrat_set", "arguments": {
            "device": "psu", "name": "VoltageLevel", "channel": "1", "value": 2}}},
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "labrat_set", "arguments": {
            "device": "psu", "name": "VoltageLevel", "channel": "1", "value": 99}}},
    )
    assert init["result"]["protocolVersion"] == "2025-06-18"
    assert {t["name"] for t in tools["result"]["tools"]} >= {"labrat_devices", "labrat_set", "labrat_stop"}
    assert not setv["result"].get("isError") and hub.devices["psu"].get("VoltageLevel", "1") == 2
    assert refused["result"]["isError"] and "limit" in refused["result"]["content"][0]["text"]


def test_mcp_stop_says_it_was_an_agent(nest):
    hub, http = nest
    rpc(Client(http.url, OPERATOR), {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                     "params": {"name": "labrat_stop", "arguments": {"reason": "smoke"}}})
    assert "stop" in hub.audit_path.read_text()


def test_token_and_init_write_what_they_say(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert cli.main(["token", "--name", "ada", "--role", "admin"]) == 0
    assert 'role = "admin"' in capsys.readouterr().out
    assert cli.main(["rat", "init"]) == 0
    text = (tmp_path / "labrat" / "rat.toml").read_text()
    assert "watchdog_s" in text
    assert cli.main(["rat", "init"]) == 2                          # never overwrites
    assert cli.main(["nest", "init"]) == 0
    assert "[[tokens]]" in (tmp_path / "labrat" / "nest.toml").read_text()


def test_the_cli_talks_to_a_nest(nest, capsys):
    _, http = nest
    assert cli.main(["set", "psu", "VoltageLevel", "4", "-c", "1", "--url", http.url, "--token", OPERATOR]) == 0
    assert cli.main(["get", "psu", "VoltageLevel", "-c", "1", "--url", http.url, "--token", OPERATOR]) == 0
    assert capsys.readouterr().out.split()[-2:] == ["4", "V"]
    assert cli.main(["set", "psu", "VoltageLevel", "40", "-c", "1", "--url", http.url, "--token", OPERATOR]) == 2


def test_the_example_configurations_are_valid():
    from pathlib import Path

    from labrat import config as conf
    root = Path(__file__).resolve().parents[1] / "examples"
    nest = conf.load("nest", root / "nest.toml")
    assert len(conf.tokens(nest)) == 3
    rat = conf.load("rat", root / "rat.toml")
    assert {"psu", "shutter", "slm", "cam", "room"} <= set(rat["devices"])


def test_the_repository_is_clean():
    import subprocess
    import sys
    from pathlib import Path
    tool = Path(__file__).resolve().parents[1] / "tools" / "check_repo.py"
    r = subprocess.run([sys.executable, str(tool)], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout
