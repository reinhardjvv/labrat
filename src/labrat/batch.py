"""Jobs through a batch scheduler: a worker on a cluster's login node hands each job to Slurm, or to any scheduler
whose commands it is told.

    backend = "slurm"               # in worker.toml; everything below is optional

    [slurm]
    partition = "gpu"
    account = "my-group"
    extra = ["--qos=normal"]        # more sbatch options
    setup = "module load cuda/12.4" # lines run before the job
    container = "apptainer exec --nv ~/labrat.sif"
    template = "~/labrat-job.sbatch"   # a batch script of your own in place of LABRAT's
    workdir = "/scratch/labrat"     # where each job's folder goes (default: the worker's state_dir)
    keep = true                     # keep each job's folder, with its batch script and output, after it ends
    gres = "gpu:{gpus}"             # how GPUs are asked for
    poll_s = 10

The worker writes the job's files and a batch script into a folder under workdir (on a file system the compute
nodes see), submits it, follows it until it ends, passes on what it prints, and cancels it on stop: first with
Ctrl-C (SIGINT), then for good. A job for several workers becomes one allocation of that many nodes, with one task
on each: srun gives every task its rank, and the first node is the leader. The commands themselves can be
changed under [slurm.commands], and backend = "batch" with a [batch] table drives another scheduler the same way.
The job's token goes into a file only its owner can read, never onto a command line.
"""
from __future__ import annotations

import math
import os
import re
import shlex
import subprocess
import time
from pathlib import Path

from .message import LabratError
from .worker import GRACE_S, Part

KEYS = {"partition", "account", "extra", "setup", "container", "template", "gres", "export", "poll_s", "workdir", "commands", "keep"}
KEEP_LOCAL = {"PATH", "HOME", "USER", "LOGNAME", "SHELL", "TMPDIR", "LANG", "LC_ALL", "LC_CTYPE", "TZ", "CUDA_VISIBLE_DEVICES",
              "RANK", "LABRAT_RANK", "LOCAL_RANK", "MASTER_ADDR", "LABRAT_LEADER", "WORLD_SIZE", "LABRAT_WORLD_SIZE"}
SLURM = {
    "submit": "sbatch --parsable {script}",
    "status": "squeue -h -j {id} -o %T",
    "accounting": "sacct -n -P -X -j {id} -o State,ExitCode",
    "interrupt": "scancel --signal=INT --full {id}",
    "cancel": "scancel {id}",
}
FINISHED = {"COMPLETED", "FAILED", "CANCELLED", "TIMEOUT", "OUT_OF_MEMORY", "NODE_FAIL", "PREEMPTED", "BOOT_FAIL", "DEADLINE"}
TEMPLATE = """#!/bin/bash -l
#SBATCH --job-name={job_name}
#SBATCH --output={workdir}/output.txt
#SBATCH --chdir={workdir}
#SBATCH --nodes={nodes}
#SBATCH --ntasks-per-node=1
{options}
{setup}
set -a; . {workdir}/job.env; set +a
export MASTER_ADDR=$(scontrol show hostnames "$SLURM_JOB_NODELIST" | head -n 1)
export LABRAT_LEADER=$MASTER_ADDR WORLD_SIZE=$SLURM_NTASKS LABRAT_WORLD_SIZE=$SLURM_NTASKS
srun --kill-on-bad-exit=1 bash -c 'export RANK=$SLURM_PROCID LABRAT_RANK=$SLURM_PROCID LOCAL_RANK=$SLURM_LOCALID; exec {container} {command}'
"""


def check_batch(cfg: dict, where: str) -> None:
    kind = "slurm" if cfg["backend"] == "slurm" else "batch"
    t = cfg.setdefault(kind, {})
    if not isinstance(t, dict) or set(t) - KEYS:
        raise LabratError("bad_request", f"{where}: [{kind}] takes {', '.join(sorted(KEYS))}")
    commands = t.get("commands") or {}
    if not isinstance(commands, dict) or set(commands) - set(SLURM):
        raise LabratError("bad_request", f"{where}: [{kind}.commands] takes {', '.join(SLURM)}")
    if kind == "batch" and not {"submit", "status", "accounting", "cancel"} <= set(commands):
        raise LabratError("bad_request", f"{where}: backend = \"batch\" needs submit, status, accounting and cancel under [batch.commands]")
    if kind == "batch" and not t.get("template"):
        raise LabratError("bad_request", f"{where}: backend = \"batch\" needs a template, the scheduler's batch script")
    if not isinstance(t.get("extra", []), list):
        raise LabratError("bad_request", f"{where}: [{kind}] extra is a list of options")
    if t.get("template") and not Path(str(t["template"])).expanduser().is_file():
        raise LabratError("bad_request", f"{where}: no template {t['template']}")


class BatchPart(Part):
    """A job handed to the scheduler as one allocation; the scheduler decides when and where it runs."""

    def __init__(self, worker, payload):
        super().__init__(worker, payload)
        kind = "slurm" if worker.cfg["backend"] == "slurm" else "batch"
        self.t = worker.cfg.get(kind) or {}
        self.commands = {**(SLURM if kind == "slurm" else {}), **(self.t.get("commands") or {})}
        base = Path(str(self.t.get("workdir") or Path(worker.cfg["state_dir"]) / "jobs")).expanduser()
        self.dir = base / f"{self.job}-{self.rank}"
        self.id: str | None = None

    def _cmd(self, name: str, **values) -> str:
        args = [fill(a, values) for a in shlex.split(self.commands[name])]
        try:
            r = subprocess.run(args, capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.SubprocessError) as e:
            raise LabratError("unavailable", f"{args[0]}: {e}") from None
        if r.returncode != 0 and name not in ("status", "interrupt"):
            raise LabratError("device", f"{' '.join(args)}: {(r.stderr or r.stdout).strip()[:500]}")
        return r.stdout.strip()

    def script(self) -> str:
        """The batch script: LABRAT's, or the owner's own template, filled in."""
        p, t = self.p, self.t
        needs = p.get("needs") or {}
        options = []
        for k in ("partition", "account"):
            if t.get(k):
                options.append(f"--{k}={t[k]}")
        if needs.get("cpus"):
            options.append(f"--cpus-per-task={int(needs['cpus'])}")
        if needs.get("memory_gb"):
            options.append(f"--mem={math.ceil(float(needs['memory_gb']) * 1024)}M")
        if needs.get("gpus"):
            options.append(f"--gres={str(t.get('gres', 'gpu:{gpus}')).format(gpus=int(needs['gpus']))}")
        if p.get("minutes"):
            options.append(f"--time={math.ceil(float(p['minutes']))}")
        if p.get("exclusive"):
            options.append("--exclusive")
        options.append(f"--export={t.get('export', 'NONE')}")
        options += [str(x) for x in t.get("extra", [])]
        command = " ".join(shlex.quote(c) for c in self.command())
        values = {"job_name": f"labrat-{self.job}-{p['name']}", "workdir": str(self.dir), "nodes": int(needs.get("nodes", 1)),
                  "options": "\n".join(f"#SBATCH {o}" for o in options), "setup": str(t.get("setup", "")),
                  "container": str(t.get("container", "")), "command": command.replace("'", "'\"'\"'"),
                  "cpus": int(needs.get("cpus") or 1), "gpus": int(needs.get("gpus") or 0), "minutes": p.get("minutes") or "",
                  "job": self.job}
        template = Path(str(t["template"])).expanduser().read_text() if t.get("template") else TEMPLATE
        return fill(template, values)

    def _env_file(self) -> None:
        """The job's own variables, readable by the worker's user only, for the batch script to load. PATH, HOME, the
        locale and the GPUs come from the compute node, its login profile and the scheduler, not from here."""
        env = self.environment()
        path = self.dir / "job.env"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            for k, v in env.items():
                if k not in KEEP_LOCAL:
                    f.write(f"{k}={shlex.quote(v)}\n")

    def execute(self) -> tuple[int | None, str | None]:
        self._env_file()
        script = self.dir / "job.sbatch"
        script.write_text(self.script())
        out = self._cmd("submit", script=str(script), workdir=str(self.dir))
        m = re.search(r"(\d+)", out.split(";")[0])
        if not m:
            raise LabratError("device", f"the scheduler answered {out!r}, with no job id")
        self.id = m.group(1)
        self.w_log(f"submitted as {self.id}")
        if self._send("started").get("stop"):
            self.stop("stopped from the nest")
        return self._follow()

    def cleanup(self) -> None:
        if not self.t.get("keep"):
            super().cleanup()

    def w_log(self, text: str) -> None:
        with self._lines_lock:
            self._lines.append(f"[labrat] {text}")

    def _tail(self, offset: int, final: bool = False) -> int:
        """Pass on what the job wrote to its output file since offset: whole lines, or everything at the end."""
        output = self.dir / "output.txt"
        if not output.is_file():
            return offset
        with open(output, "rb") as f:
            f.seek(offset)
            data = f.read()
        cut = len(data) if final else data.rfind(b"\n") + 1
        if cut:
            with self._lines_lock:
                self._lines += data[:cut].decode(errors="replace").splitlines()
        return offset + cut

    def _follow(self) -> tuple[int | None, str | None]:
        poll = float(self.t.get("poll_s", 10))
        offset, last, interrupted = 0, 0.0, None
        while True:
            offset = self._tail(offset)
            if self._flush():
                self.stop("stopped from the nest")
            if self._why is not None:
                now = time.monotonic()
                if interrupted is None:
                    interrupted = now
                    if "interrupt" in self.commands:
                        self._cmd("interrupt", id=self.id)
                    else:
                        self._cmd("cancel", id=self.id)
                elif now - interrupted > GRACE_S and interrupted > 0:
                    self._cmd("cancel", id=self.id)
                    interrupted = -1.0                                   # cancelled for good; wait for it to end
            if time.monotonic() - last >= poll:
                last = time.monotonic()
                state = self._cmd("status", id=self.id).split("\n")[0].strip().upper()
                if not state or state.split()[0] in FINISHED:
                    self._tail(offset, final=True)
                    return self._result()
            time.sleep(min(0.5, poll))

    def _result(self) -> tuple[int | None, str | None]:
        """The exit code from accounting, which answers 'STATE|EXIT:SIGNAL'."""
        for _ in range(5):
            row = self._cmd("accounting", id=self.id).split("\n")[0]
            if "|" in row:
                state, code = (x.strip() for x in row.split("|")[:2])
                state = (state.split() or ["UNKNOWN"])[0].upper()
                number = code.split(":")[0]
                exit_code = int(number) if number.isdigit() else None
                why = self._why if self._why and self._why != "stopped from the nest" else None
                if state == "COMPLETED":
                    return exit_code or 0, why
                return exit_code or 1, why or (None if self._why else f"the scheduler reports {state.lower()}")
            time.sleep(2)
        return None, "the scheduler has no record of how the job ended"


def fill(text: str, values: dict) -> str:
    """{name} replaced by its value for the names LABRAT knows; anything else, such as ${HOME}, left as it is."""
    return re.sub(r"\{(\w+)\}", lambda m: str(values[m.group(1)]) if m.group(1) in values else m.group(0), text)
