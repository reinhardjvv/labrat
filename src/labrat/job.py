"""Inside a job: which part this is, and how it reaches the lab.

    from labrat import job

    lab = job.client()                   # the nest, as the person who sent the job
    if job.rank == 0:
        lab.note(f"training on {job.world_size} workers")

A part learns its place from its environment. For a job across several workers, MASTER_ADDR, MASTER_PORT, RANK
and WORLD_SIZE are set as well, so torch.distributed and similar libraries find each other with no more set-up.
Outside a job, the values fall back to a single part with rank 0.
"""
from __future__ import annotations

import os

from .client import Client

id = int(os.environ.get("LABRAT_JOB", "0"))
name = os.environ.get("LABRAT_JOB_NAME", "")
rank = int(os.environ.get("LABRAT_RANK", "0"))
world_size = int(os.environ.get("LABRAT_WORLD_SIZE", "1"))
leader = os.environ.get("LABRAT_LEADER", "127.0.0.1")
worker = os.environ.get("LABRAT_WORKER", "")


def client() -> Client:
    """The nest, with this part's own token: the person's rights, and only the devices the job names."""
    return Client()
