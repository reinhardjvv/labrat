"""Inside a job: which part this is, and how it reaches the lab.

    from labrat import job

    lab = job.client()                   # the nest, as the person who sent the job
    if job.rank == 0:
        lab.note(f"training on {job.world_size} workers")
    frames = job.path("raw")             # where this job's raw data goes, on the store the lab or job chose

A part learns its place from its environment. For a job across several workers, MASTER_ADDR, MASTER_PORT, RANK
and WORLD_SIZE are set as well, so torch.distributed and similar libraries find each other with no more set-up.
Outside a job, the values fall back to a single part with rank 0.
"""
from __future__ import annotations

import os
from pathlib import Path

from .client import Client
from .message import LabratError

id = int(os.environ.get("LABRAT_JOB", "0"))
name = os.environ.get("LABRAT_JOB_NAME", "")
rank = int(os.environ.get("LABRAT_RANK", "0"))
world_size = int(os.environ.get("LABRAT_WORLD_SIZE", "1"))
leader = os.environ.get("LABRAT_LEADER", "127.0.0.1")
worker = os.environ.get("LABRAT_WORKER", "")


def client() -> Client:
    """The nest, with this part's own token: the person's rights, and only the devices the job names."""
    return Client()


def path(purpose: str = "data") -> Path:
    """The folder this job writes one kind of result to, on the store chosen for it (docs/workers.md)."""
    value = os.environ.get(f"LABRAT_STORE_{purpose.upper()}")
    if not value:
        have = sorted(k[13:].lower() for k in os.environ if k.startswith("LABRAT_STORE_"))
        raise LabratError("not_found", f"this job has no store for {purpose!r}; it has {', '.join(have) or 'none'} "
                          f"(labrat job run --store {purpose}=<store>)")
    return Path(value)


def paths() -> dict[str, Path]:
    """Every kind of result this job has a folder for."""
    return {k[13:].lower(): Path(v) for k, v in os.environ.items() if k.startswith("LABRAT_STORE_")}
