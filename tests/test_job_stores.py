import json
import threading
import time

import pytest
from conftest import OPERATOR, SIM, TOKENS, load

from labrat.client import Client
from labrat.http import Server
from labrat.message import LabratError
from labrat.nest import Nest
from labrat.worker import Worker, check_config


@pytest.fixture
def lab(tmp_path):
    for d in ("nas", "raw", "olu-raw"):
        (tmp_path / d).mkdir()
    hub = Nest(load(tmp_path, "nest", TOKENS + SIM + f'''
[stores.nas]
path = "{tmp_path / 'nas'}"
[experiments]
store = "nas"
[jobs]
stores = {{ data = "nas", raw = "raw" }}
'''))
    server = Server(hub, "127.0.0.1", 0).start()
    url = f"http://127.0.0.1:{server.port}"
    started = []

    def worker(name="gpu-box", stores=("nas", "raw", "olu-raw"), **cfg):
        w = Worker(check_config({"name": name, "state_dir": str(tmp_path / f"worker-{name}"), "nest": url,
                                 "stores": {s: {"path": str(tmp_path / s), **(cfg.pop(f"{s}_extra", {}))} for s in stores}, **cfg}))
        w.join(url, OPERATOR)
        w.wait = 1.0
        threading.Thread(target=w.run_forever, daemon=True).start()
        started.append(w)
        deadline = time.time() + 10
        while not hub.workers.online(name) and time.time() < deadline:
            time.sleep(0.05)
        return w
    yield hub, Client(url, OPERATOR), worker, tmp_path
    for w in started:
        w.close()
    server.close()
    hub.close()


def wait_for(client, job_id, timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        j = client.job(job_id)
        if j["status"] in ("done", "failed", "stopped", "cancelled"):
            return j
        time.sleep(0.1)
    raise AssertionError(f"job {job_id} is still {j['status']}")


WRITE = """
from labrat import job
(job.path("raw") / "frames.bin").write_bytes(b"x" * 1000)
(job.path() / "fit.txt").write_text("slope 2")
print("wrote", sorted(job.paths()))
"""


def test_results_go_straight_to_the_stores_for_each_purpose(lab):
    hub, olu, worker, tmp = lab
    worker(hash=True)
    s = tmp / "fit.py"
    s.write_text(WRITE)
    j = wait_for(olu, olu.run_job(s)["id"])
    assert j["status"] == "done", olu.job_output(j["id"])["text"]
    data, raw = tmp / "nas" / "olu" / f"fit-{j['id']}", tmp / "raw" / "olu" / f"fit-{j['id']}"
    assert (raw / "frames.bin").stat().st_size == 1000 and (data / "fit.txt").read_text() == "slope 2"
    assert "wrote ['data', 'raw']" in (data / "output.txt").read_text()
    record = json.loads((data / f"job-{j['id']}.json").read_text())
    assert record["person"] == "olu" and record["worker"] == "gpu-box"
    files = j["parts"][0]["files"]
    assert files["raw"]["store"] == "raw" and files["raw"]["folder"] == f"olu/fit-{j['id']}"
    assert files["raw"]["files"][0]["path"] == "frames.bin" and len(files["raw"]["files"][0]["sha256"]) == 64


def test_a_job_chooses_its_own_stores_and_output(lab):
    hub, olu, worker, tmp = lab
    worker()
    s = tmp / "fit.py"
    s.write_text(WRITE)
    j = wait_for(olu, olu.run_job(s, stores={"raw": "olu-raw:runs/%x/%j"}, output="nas:logs/%x-%j.out")["id"])
    assert j["status"] == "done", j
    assert (tmp / "olu-raw" / "runs" / "fit" / str(j["id"]) / "frames.bin").is_file()
    assert "wrote" in (tmp / "nas" / "logs" / f"fit-{j['id']}.out").read_text()


def test_paths_stay_inside_their_store(lab):
    hub, olu, worker, tmp = lab
    worker()
    s = tmp / "x.py"
    s.write_text("print(1)\n")
    for bad in ({"raw": "raw:../elsewhere"}, {"raw": "raw:/etc"}, {"raw": "raw:%u/%q"}, {"Raw": "raw"}):
        with pytest.raises(LabratError):
            olu.run_job(s, stores=bad)


def test_another_jobs_folder_is_never_written_into(lab):
    hub, olu, worker, tmp = lab
    worker()
    (tmp / "raw" / "shared").mkdir()
    (tmp / "raw" / "shared" / "old.bin").write_text("someone else's")
    s = tmp / "fit.py"
    s.write_text(WRITE)
    j = wait_for(olu, olu.run_job(s, stores={"raw": "raw:shared"})["id"])
    assert j["status"] == "failed" and "already holds another job's files" in j["error"]
    assert (tmp / "raw" / "shared" / "old.bin").read_text() == "someone else's"


def test_a_job_inside_an_open_experiment_writes_into_it(lab):
    hub, olu, worker, tmp = lab
    worker()
    exp = olu.start_experiment("sweep")
    s = tmp / "fit.py"
    s.write_text(WRITE)
    j = wait_for(olu, olu.run_job(s)["id"])
    assert j["status"] == "done"
    folder = tmp / "nas" / exp["folder"]
    assert (folder / "fit.txt").is_file() and (folder / f"job-{j['id']}.json").is_file()
    names = [f["name"] for f in olu.files(exp["folder"].split("/", 1)[1])]
    assert "fit.txt" in names


def test_jobs_wait_for_a_worker_that_has_their_stores(lab):
    hub, olu, worker, tmp = lab
    worker(stores=("nas",))
    s = tmp / "fit.py"
    s.write_text(WRITE)
    with pytest.raises(LabratError) as e:
        olu.run_job(s)
    assert "has store raw" in e.value.message


def test_a_store_can_copy_results_on_after_the_job(lab):
    hub, olu, worker, tmp = lab
    copies = tmp / "copies"
    copies.mkdir()
    worker(raw_extra={"copy": f"cp -R {{src}} {copies}"})
    s = tmp / "fit.py"
    s.write_text(WRITE)
    j = wait_for(olu, olu.run_job(s)["id"])
    assert j["status"] == "done" and (copies / f"fit-{j['id']}" / "frames.bin").is_file()


def test_the_parts_of_a_group_share_a_folder_and_keep_their_own_output(lab):
    hub, olu, worker, tmp = lab
    worker("box-a")
    worker("box-b")
    s = tmp / "group.py"
    s.write_text("from labrat import job\n(job.path() / f'part-{job.rank}.txt').write_text('ok')\nprint('rank', job.rank)\n")
    j = wait_for(olu, olu.run_job(s, nodes=2)["id"])
    folder = tmp / "nas" / "olu" / f"group-{j['id']}"
    assert j["status"] == "done", j.get("error")
    assert {p.name for p in folder.iterdir()} >= {"part-0.txt", "part-1.txt", "output-0.txt", "output-1.txt"}
    assert "rank 1" in (folder / "output-1.txt").read_text()
