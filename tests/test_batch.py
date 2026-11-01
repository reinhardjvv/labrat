import os
import stat
import threading
import time

import pytest
from conftest import OPERATOR

from labrat.batch import fill
from labrat.client import Client
from labrat.message import LabratError
from labrat.worker import Worker, check_config

FAKE = {
    # sbatch: runs the batch script in the background as the given job id, output to the folder's output.txt
    "sbatch": r'''#!/bin/bash
state="$FAKE_SLURM"; script="${@: -1}"; dir=$(dirname "$script")
id=$(( $(cat "$state/next" 2>/dev/null || echo 100) + 1 )); echo $id > "$state/next"
cp "$script" "$state/$id.sbatch"
nodes=$(grep -o -- '--nodes=[0-9]*' "$script" | cut -d= -f2)
( cd "$dir"; SLURM_JOB_NODELIST="node[1-$nodes]" SLURM_NTASKS=$nodes SLURM_NNODES=$nodes \
  bash "$script" > "$dir/output.txt" 2>&1; echo $? > "$state/$id.code" ) < /dev/null > /dev/null 2>&1 &
echo $! > "$state/$id.pid"
echo "$id;cluster"
''',
    "squeue": r'''#!/bin/bash
id="${@: -3:1}"; state="$FAKE_SLURM"
[ -f "$state/$id.code" ] && exit 0
kill -0 $(cat "$state/$id.pid") 2>/dev/null && echo RUNNING
''',
    "sacct": r'''#!/bin/bash
id=$(echo "$@" | grep -o -- '-j [0-9]*' | cut -d' ' -f2); state="$FAKE_SLURM"
code=$(cat "$state/$id.code" 2>/dev/null || echo 1)
if [ -f "$state/$id.cancelled" ]; then echo "CANCELLED by 0|$code:0"
elif [ "$code" = 0 ]; then echo "COMPLETED|0:0"; else echo "FAILED|$code:0"; fi
''',
    "scancel": r'''#!/bin/bash
id="${@: -1}"; state="$FAKE_SLURM"; touch "$state/$id.cancelled"
pkill -INT -P $(cat "$state/$id.pid") 2>/dev/null; pkill -INT -f "fake-slurm-task-$id" 2>/dev/null
[ "$1" = "--signal=INT" ] || pkill -KILL -P $(cat "$state/$id.pid") 2>/dev/null
exit 0
''',
    # srun: one task per node, each with its own SLURM_PROCID
    "srun": r'''#!/bin/bash
shift; pids=""
for i in $(seq 0 $(( SLURM_NTASKS - 1 ))); do SLURM_PROCID=$i SLURM_LOCALID=0 "$@" & pids="$pids $!"; done
code=0; for p in $pids; do wait $p || code=$?; done; exit $code
''',
    "scontrol": "#!/bin/bash\nfor i in $(seq 1 $SLURM_NNODES); do echo node$i; done\n",
}


@pytest.fixture
def cluster(nest, tmp_path, monkeypatch):
    hub, http = nest
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    for name, text in FAKE.items():
        (bin_ / name).write_text(text)
        (bin_ / name).chmod(0o755)
    state = tmp_path / "slurm"
    state.mkdir()
    monkeypatch.setenv("PATH", f"{bin_}:{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_SLURM", str(state))
    w = Worker(check_config({"name": "cluster", "state_dir": str(tmp_path / "worker"), "nest": http.url, "backend": "slurm",
                             "slurm": {"partition": "gpu", "account": "lab", "setup": "echo set up", "poll_s": 0.2, "keep": True,
                                       "extra": ["--qos=normal"]}}))
    w.join(http.url, OPERATOR)
    w.wait = 1.0
    threading.Thread(target=w.run_forever, daemon=True).start()
    deadline = time.time() + 10
    while not hub.workers.online("cluster") and time.time() < deadline:
        time.sleep(0.05)
    yield hub, Client(http.url, OPERATOR), tmp_path, state
    w.close()


def wait_for(client, job_id, timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        j = client.job(job_id)
        if j["status"] in ("done", "failed", "stopped", "cancelled"):
            return j
        time.sleep(0.1)
    raise AssertionError(f"job {job_id} is still {j['status']}")


def test_a_job_goes_through_sbatch_with_what_it_needs(cluster):
    hub, olu, tmp, state = cluster
    s = tmp / "fit.py"
    s.write_text("import os\nprint('fitting as rank', os.environ['RANK'], 'token' if os.environ.get('LABRAT_TOKEN') else 'no token')\n")
    j = wait_for(olu, olu.run_job(s, gpus=2, cpus=4, memory_gb=8, minutes=30)["id"])
    text = olu.job_output(j["id"])["text"]
    assert j["status"] == "done", text
    assert "submitted as 101" in text and "set up" in text and "fitting as rank 0 token" in text
    batch = (state / "101.sbatch").read_text()
    for line in ("--partition=gpu", "--account=lab", "--cpus-per-task=4", "--mem=8192M", "--gres=gpu:2", "--time=30",
                 "--export=NONE", "--qos=normal", "--nodes=1"):
        assert f"#SBATCH {line}" in batch
    env = tmp / "worker" / "jobs" / f"{j['id']}-0" / "job.env"
    assert stat.S_IMODE(env.stat().st_mode) == 0o600
    assert "LABRAT_TOKEN=" in env.read_text() and "PATH=" not in env.read_text()
    from labrat.jobs import part_token
    token = part_token(hub.workers.entries["cluster"]["key"], j["id"], 0)
    assert token in env.read_text() and token not in batch               # the token is never in the script


def test_a_job_for_several_nodes_is_one_allocation(cluster):
    hub, olu, tmp, state = cluster
    s = tmp / "train.py"
    s.write_text("import os\nos.write(1, f\"rank {os.environ['RANK']} of {os.environ['WORLD_SIZE']} leader {os.environ['MASTER_ADDR']}\\n\".encode())\n")
    j = wait_for(olu, olu.run_job(s, nodes=2, on=["cluster"])["id"])
    text = olu.job_output(j["id"])["text"]
    assert j["status"] == "done" and len(j["parts"]) == 1, text
    assert "rank 0 of 2 leader node1" in text and "rank 1 of 2 leader node1" in text


def test_a_failing_job_reports_its_exit_code(cluster):
    hub, olu, tmp, state = cluster
    s = tmp / "bad.py"
    s.write_text("raise SystemExit(3)\n")
    j = wait_for(olu, olu.run_job(s)["id"])
    assert j["status"] == "failed" and j["exit_code"] == 3


def test_stop_cancels_the_scheduler_job(cluster):
    hub, olu, tmp, state = cluster
    s = tmp / "slow.py"
    s.write_text("import time\nprint('started', flush=True)\ntime.sleep(60)\n")
    j = olu.run_job(s)
    deadline = time.time() + 15
    while "started" not in olu.job_output(j["id"])["text"] and time.time() < deadline:
        time.sleep(0.1)
    olu.stop_job(j["id"])
    j = wait_for(olu, j["id"])
    assert j["status"] == "stopped" and (state / "101.cancelled").exists()


def test_batch_settings_are_checked(tmp_path):
    with pytest.raises(LabratError):
        check_config({"backend": "slurm", "slurm": {"partiton": "gpu"}})
    with pytest.raises(LabratError):
        check_config({"backend": "batch", "batch": {"commands": {"submit": "qsub {script}"}}})
    t = tmp_path / "job.pbs"
    t.write_text("#PBS -N {job_name}\n")
    cfg = check_config({"backend": "batch", "batch": {"template": str(t), "commands": {
        "submit": "qsub {script}", "status": "qstat {id}", "accounting": "qacct {id}", "cancel": "qdel {id}"}}})
    assert cfg["slots"] == 32


def test_templates_keep_shell_variables():
    assert fill("cd ${HOME}/{job_name} && echo {unknown}", {"job_name": "x"}) == "cd ${HOME}/x && echo {unknown}"
