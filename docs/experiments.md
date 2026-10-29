# Experiments

An experiment keeps one piece of work together in its owner's folder: the commands they sent, every
instrument's settings at the start and at the end, their notes and the recordings they started. Experiments
are optional. A nest without an `[experiments]` table keeps none, and everything else works as before.

## Turning them on

```toml
[stores.nas]
path = "/mnt/labnas/labrat"

[experiments]
store = "nas"
snapshot = true          # save every device's identity, description and readings at start and stop
```

Any [store](recordings.md#stores) works: a local disk, a network share, or a network share with a local buffer.

## The folder

```
/mnt/labnas/labrat/
  ada/
    2026-10-29_supply-sweep/
      experiment.json                      who, when, what it is for, notes, recordings, how it ended
      instruments.json                     every device at the start and at the end
      log.jsonl                            every command ada (or a token acting for her) sent
      scope-traces-20261029-141203.jsonl   recordings started during the experiment
      scope-traces-20261029-141203.meta.json
  ben/
    2026-10-30_noise-floor/
```

The person is the name of the token that sent the request. An experiment name that already exists for
that day gets `-2`, `-3` and so on. Data is never overwritten. Each person has one experiment open at a time.
An open experiment carries on through a restart of the nest.

`log.jsonl` holds the owner's commands only. Changes made by other people in the meantime still show in the
end snapshot in `instruments.json`, and in the nest's own audit log.

## Using them

```bash
labrat experiment start supply-sweep supply from 1 to 5 V
labrat record start scope-traces            # goes into the experiment's folder, no store needed
labrat experiment note "3 V is steady"
labrat experiment                           # the open experiment, its notes and recordings
labrat experiment stop

labrat experiments                          # yours, newest first
labrat files                                # your experiment folders
labrat files 2026-10-29_supply-sweep        # the files in one
labrat files 2026-10-29_supply-sweep/log.jsonl
labrat files 2026-10-29_supply-sweep/scope-traces-20261029-141203.jsonl --get
```

From Python:

```python
from labrat.client import Client

lab = Client()
with lab.experimenting("supply-sweep", "supply from 1 to 5 V"):
    for v in (1, 2, 3, 4, 5):
        lab.set("psu", "VoltageLevel", v, channel="1")
        lab.note(f"{v} V set")
lab.download("2026-10-29_supply-sweep/log.jsonl")
```

The block stops the experiment even when it fails.

## AI agents working for a person

A token can act for a person:

```toml
[[tokens]]
name = "claude-ada"
role = "operator"
for = "ada"
sha256 = "..."
```

The agent then works in ada's folder. Ada sees its open experiment as her own and can stop it. Every log
entry and metadata file names `claude-ada` as the sender and `ada` as the person. A token that acts for
someone can never approve a request. Approvals stay with people.

Through MCP the agent has `labrat_experiment` to start, note and stop, and `labrat_files` to read results
back, 256 KB at a time.

## Who reads what

People list and read their own folders only. Paths are relative to the person's folder. A path with `..`, or
a symbolic link that leads out of the folder, is refused. Admins may read anyone's with `--person` (the
`person` argument), and `labrat experiments --person '*'` lists everyone's.

Starting, stopping and writing notes needs an operator token. A viewer can read their own folder.
