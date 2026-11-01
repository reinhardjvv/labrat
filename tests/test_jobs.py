import os
import threading
import time

import pytest
from conftest import ADMIN, OPERATOR, OTHER

from labrat.client import Client
from labrat.message import LabratError
from labrat.worker import Worker, check_config


@pytest.fixture
def lab(nest, tmp_path):
    """A nest with a running worker of olu's, and a way to add more."""
    hub, http = nest
    started = []

    def worker(name="gpu-box", **cfg):
        w = Worker(check_config({"name": name, "state_dir": str(tmp_path / f"worker-{name}"), "nest": http.url, **cfg}))
        w.join(http.url, OPERATOR)
        if cfg.get("shared"):
            http.post({"op": "workers", "name": name, "args": {"action": "share"}})
        w.wait = 1.0
        threading.Thread(target=w.run_forever, daemon=True).start()
        started.append(w)
        deadline = time.time() + 10
        while not hub.workers.online(name) and time.time() < deadline:
            time.sleep(0.05)
        return w
    olu = Client(http.url, OPERATOR)
    yield hub, http, olu, worker, tmp_path
    for w in started:
        w.close()


def script(tmp_path, name, text):
    p = tmp_path / name
    p.write_text(text)
    return p


def wait_for(client, job_id, states=("done", "failed", "stopped", "cancelled"), timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        j = client.job(job_id)
        if j["status"] in states:
            return j
        time.sleep(0.1)
    raise AssertionError(f"job {job_id} is still {j['status']}")


def output(client, job_id, rank=0):
    return client.job_output(job_id, rank)["text"]


def test_a_job_runs_as_its_person_and_changes_only_its_devices(lab):
    hub, http, olu, worker, tmp = lab
    worker()
    s = script(tmp, "probe.py", """
from labrat import job
lab = job.client()
print("volts", lab.get("psu", "VoltageLevel", channel="1"))
for dev in ("psu", "relays"):
    try:
        lab.set(dev, "VoltageLevel" if dev == "psu" else "heater", 3 if dev == "psu" else True, channel="1" if dev == "psu" else None)
        print(dev, "changed")
    except Exception as e:
        print(dev, "refused", e)
try:
    lab.set("psu", "VoltageLevel", 20, channel="1")
except Exception as e:
    print("limit", e)
try:
    lab.request("job", args={"action": "submit", "spec": {"script": "x.py"}})
except Exception as e:
    print("nested", e)
print("rank", job.rank, "of", job.world_size)
""")
    j = olu.run_job(s, devices=["psu"])
    assert j["status"] in ("queued", "running")
    j = wait_for(olu, j["id"])
    text = output(olu, j["id"])
    assert j["status"] == "done", text
    assert "psu changed" in text and "relays refused" in text and "limit" in text and "above the limit" in text
    assert "nested" in text and "may not" in text and "rank 0 of 1" in text
    assert hub.devices["psu"].get("VoltageLevel", "1") == 3
    audit = (hub.audit_path).read_text()
    assert '"job": ' in audit and '"who": "olu"' in audit
    assert not any(p.job for p in hub.tokens.values())                # the part's token ended with it


def test_a_job_gets_a_clean_environment(lab, monkeypatch):
    hub, http, olu, worker, tmp = lab
    monkeypatch.setenv("OWNER_SECRET", "do-not-pass")
    monkeypatch.setenv("CUDA_HOME", "/opt/cuda")
    worker(pass_env=["CUDA_HOME"])
    s = script(tmp, "env.py", "import os\nprint(sorted(os.environ))\nprint(os.environ.get('CUDA_HOME'), os.environ.get('MINE'))\n")
    j = wait_for(olu, olu.run_job(s, env={"MINE": "yes"})["id"])
    text = output(olu, j["id"])
    assert "OWNER_SECRET" not in text and "/opt/cuda yes" in text and "LABRAT_TOKEN" in text


def test_a_job_across_two_workers_starts_together_and_stops_together(lab):
    hub, http, olu, worker, tmp = lab
    worker("box-a")
    worker("box-b")
    s = script(tmp, "group.py", """
import os, sys, time
print("rank", os.environ["RANK"], "of", os.environ["WORLD_SIZE"], "leader", os.environ["MASTER_ADDR"], os.environ["MASTER_PORT"])
sys.stdout.flush()
if os.environ["RANK"] == "1":
    sys.exit(3)
try:
    time.sleep(60)
except KeyboardInterrupt:
    print("rank 0 cleaned up")
""")
    j = wait_for(olu, olu.run_job(s, nodes=2)["id"])
    assert j["status"] == "failed" and sorted(j["workers"]) == ["box-a", "box-b"]
    parts = {p["rank"]: p for p in j["parts"]}
    assert parts[1]["status"] == "failed" and parts[1]["exit_code"] == 3 and parts[0]["status"] == "stopped"
    assert "rank 0 cleaned up" in output(olu, j["id"], 0)
    assert "rank 1 of 2" in output(olu, j["id"], 1) and "29500" in output(olu, j["id"], 1)


def test_jobs_wait_for_a_free_slot_and_can_be_stopped(lab):
    hub, http, olu, worker, tmp = lab
    worker()
    s = script(tmp, "slow.py", "import time\ntry:\n    time.sleep(60)\nexcept KeyboardInterrupt:\n    print('stopped cleanly')\n")
    first = olu.run_job(s)
    second = olu.run_job(s)
    time.sleep(1.5)
    assert olu.job(second["id"])["status"] == "queued"
    olu.stop_job(first["id"])
    j = wait_for(olu, first["id"])
    assert j["status"] == "stopped" and "stopped cleanly" in output(olu, first["id"])
    assert wait_for(olu, second["id"], ("running",))["workers"] == ["gpu-box"]
    olu.stop_job(second["id"])
    wait_for(olu, second["id"])
    third = olu.run_job(s, on=["gpu-box"])
    assert olu.stop_job(third["id"])["status"] in ("cancelled", "running")
    assert wait_for(olu, third["id"])["status"] in ("cancelled", "stopped")


def test_a_personal_worker_takes_only_its_owners_jobs(lab):
    hub, http, olu, worker, tmp = lab
    worker()
    s = script(tmp, "hi.py", "print('hi')\n")
    oth = Client(http.url, OTHER)
    with pytest.raises(LabratError) as e:
        oth.run_job(s, on=["gpu-box"])
    assert e.value.code == "forbidden"
    with pytest.raises(LabratError) as e:
        oth.run_job(s)
    assert "no worker takes" in e.value.message
    with pytest.raises(LabratError) as e:
        oth.job(olu.run_job(s)["id"])
    assert e.value.code == "forbidden"


def test_a_shared_worker_runs_other_peoples_jobs(lab):
    hub, http, olu, worker, tmp = lab
    worker(shared=True)
    oth = Client(http.url, OTHER)
    j = wait_for(oth, oth.run_job(script(tmp, "hi.py", "print('hi from oth')\n"))["id"])
    assert j["status"] == "done" and j["person"] == "oth" and "hi from oth" in output(oth, j["id"])


def test_an_agents_job_can_wait_for_its_person(lab, tmp_path):
    hub, http, olu, worker, tmp = lab
    from labrat.hub import Rule
    hub.rules.append(Rule.from_dict({"who": ["claude-*"], "ops": ["job"]}))
    worker()
    _, r = http.post({"op": "users", "name": "claude-olu", "args": {"action": "add", "role": "operator", "for": "olu"}})
    agent = Client(http.url, r["value"]["token"])
    with pytest.raises(LabratError) as e:
        agent.run_job(script(tmp, "a.py", "print('agent job')\n"))
    assert e.value.code == "pending"
    with pytest.raises(LabratError):                                  # an agent never approves
        agent.approve(e.value.extra["approval"])
    done = olu.approve(e.value.extra["approval"])
    j = wait_for(olu, done["result"]["value"]["id"])
    assert j["status"] == "done" and j["by"] == "claude-olu" and j["person"] == "olu"
    assert olu.jobs()[0]["id"] == j["id"]


def test_a_worker_that_takes_installed_scripts_only(lab):
    hub, http, olu, worker, tmp = lab
    folder = tmp / "installed"
    folder.mkdir()
    (folder / "known.py").write_text("print('installed script ran')\n")
    worker(accept="scripts", scripts=str(folder))
    j = wait_for(olu, olu.run_job("known.py", installed=True)["id"])
    assert j["status"] == "done" and "installed script ran" in output(olu, j["id"])
    sent = olu.run_job(script(tmp, "sent.py", "print('should not run')\n"))
    time.sleep(1.5)
    assert olu.job(sent["id"])["status"] == "queued"                  # no worker takes code sent with a job
    olu.stop_job(sent["id"])
    with pytest.raises(LabratError):
        olu.run_job("../escape.py", installed=True)


def test_a_job_that_runs_too_long_is_stopped(lab):
    hub, http, olu, worker, tmp = lab
    worker()
    s = script(tmp, "long.py", "import time\ntime.sleep(60)\n")
    j = wait_for(olu, olu.run_job(s, minutes=0.02)["id"])
    assert j["status"] == "failed" and "time limit" in j["error"]


def test_admins_see_everyones_jobs(lab):
    hub, http, olu, worker, tmp = lab
    worker()
    j = olu.run_job(script(tmp, "x.py", "print(1)\n"))
    wait_for(olu, j["id"])
    assert [r["id"] for r in Client(http.url, ADMIN).jobs("*")] == [j["id"]]
    assert Client(http.url, OTHER).jobs() == []


@pytest.mark.skipif(os.name != "posix", reason="POSIX signals")
def test_job_records_survive_a_restart_of_the_nest(lab, tmp_path):
    hub, http, olu, worker, tmp = lab
    worker()
    j = wait_for(olu, olu.run_job(script(tmp, "x.py", "print(1)\n"))["id"])
    from labrat.jobs import Jobs
    again = Jobs(hub, hub.workers, tmp_path / "nest")
    assert again.jobs[j["id"]]["status"] == "done" and again._next == j["id"] + 1


def test_the_command_line_and_mcp_start_jobs(lab, capsys):
    hub, http, olu, worker, tmp = lab
    worker()
    from labrat import cli, mcp
    s = script(tmp, "args.py", "import sys\nprint('got', sys.argv[1:])\n")
    assert cli.main(["job", "run", str(s), "--url", http.url, "--token", OPERATOR, "--", "--steps", "3"]) == 0
    jid = int(capsys.readouterr().out.split()[1])
    assert wait_for(olu, jid)["status"] == "done" and "got ['--steps', '3']" in output(olu, jid)
    assert cli.main(["jobs", "--url", http.url, "--token", OPERATOR]) == 0
    assert "args" in capsys.readouterr().out
    r = mcp.call_tool(olu, "labrat_job", {"action": "submit", "script": "agent.py", "code": "print(6 * 7)"})
    assert wait_for(olu, r["id"])["status"] == "done" and "42" in mcp.call_tool(olu, "labrat_job", {"action": "output", "id": r["id"]})["text"]
    assert mcp.call_tool(olu, "labrat_workers", {})[0]["name"] == "gpu-box"


def test_the_audit_log_keeps_long_values_as_their_hash(nest):
    import hashlib
    import json
    hub, _ = nest
    from labrat.hub import Principal
    frame = "A" * 5000
    hub.audit(Principal("olu", "operator"), {"op": "call", "device": "slm", "name": "ShowImage", "args": {"data": frame}}, True)
    entry = json.loads(hub.audit_path.read_text().splitlines()[-1])
    assert entry["args"]["data"] == {"bytes": 5000, "sha256": hashlib.sha256(frame.encode()).hexdigest()}
