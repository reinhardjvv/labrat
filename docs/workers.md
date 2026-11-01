# Workers

A worker is a computer that runs jobs for a nest: a GPU workstation, a laptop, a compute box with a fast link to
another one, or the login node of a cluster. It connects out to its nest and asks for work, so nothing on it
listens for connections, and its owner decides in `worker.toml` what it takes.

## Joining

On the computer, with LABRAT installed (`pipx install labrat-lab`):

```bash
labrat setup
```

`labrat setup` looks at the computer (CPUs, memory, GPUs, network shares, displays and instruments plugged in),
finds the nest on the network, and asks what the computer should be: a worker, a rat for the devices it found, or
both. It asks who may use it, when it takes jobs, how many CPUs they may use and which network shares are the
lab's stores, with defaults that suit the computer (a laptop takes jobs only while nobody is logged in). It then
shows a summary and changes nothing until you say yes.

The computer never needs anyone's token. It makes its own keys and shows a code:

```text
  ┌─ Approve this computer ───────────────────────────────────┐
  │  Code           HJKL-4821                                 │
  │  Fingerprint    3f2a-91c0-77de                            │
  │  On a signed-in computer, run:                            │
  │  labrat approve HJKL-4821                                 │
  └───────────────────────────────────────────────────────────┘
```

Whoever approves the code on a computer that is signed in sees its name, fingerprint and what it asks for
(`labrat joins` lists those waiting). Compare the fingerprint with the one on the computer's screen. Anyone with
an operator token may approve a computer as their own personal worker; joining as shared, for someone else
(`--owner`) or as a rat needs an admin. A code runs out after ten minutes, and `labrat deny <code>` refuses one.

For many computers at once, an admin makes a join key, and each computer joins with it without waiting:

```bash
labrat key new --uses 20 --hours 24 --shared --owner lab --role worker --role rat
labrat setup --key lk_...                 # on each computer
labrat key                                # what each key has left
labrat key revoke 3f9a1c2e
```

A key says how many computers it lets in, until when, as what, whose they are and whether they are shared.
Revoking it stops new joins; computers that already joined stay.

Once approved, setup writes the configuration, starts the services and lists each step as it does it: systemd
system services as root (running as the `labrat` user), systemd user services otherwise, launchd on a Mac.

| Option | Meaning |
| --- | --- |
| `--url` | the nest, when discovery cannot see it |
| `--key` | a join key: no approval needed |
| `--role`, `--name`, `--shared`, `--when`, `--hours`, `--cpus`, `--store NAME=PATH` | answers given up front |
| `--share-hardware`, `--keep-hardware` | share the computer's hardware with the lab, or not |
| `--no-benchmark` | skip the disk speed test |
| `--yes` | no questions: every default and the answers given |
| `--dry-run` | the summary only; nothing is written, joined or started |
| `--no-service` | write and join, but start no service |
| `--undo` | stop and remove the services, remove the files setup wrote, and take the worker out of the nest |

Setup lists everything it writes in `setup.json` in the state folder, which `--undo` reads. The steps behind it
can still be run by hand: `labrat worker init`, `labrat worker join` with your own token in `LABRAT_TOKEN`, and
`labrat worker` to run it (`labrat service worker` prints a systemd unit).

A worker joins as **personal** or **shared**:

- A personal worker runs its owner's jobs only, from the moment it joins.
- `labrat worker join --shared` (or `shared = true`) offers it to other people as well. It runs their jobs only
  after an admin agrees with `labrat worker share <name>`, and only for the people its owner lists in `people`.
  A laptop therefore never starts taking other people's work, or their data, without both its owner and an
  admin agreeing.

The owner or an admin takes a worker back with `labrat worker unshare <name>` or removes it with
`labrat worker remove <name>`. An AI agent's token cannot join a computer: a person does that.

```bash
labrat workers              # every worker, its owner, sharing, state and what it has
labrat worker show gpu-box  # one worker in full
labrat worker key           # this computer's worker name and key fingerprint
```

## What a computer is

`labrat setup` describes the computer before it asks anything, on Linux (x86 and ARM boards such as a
Raspberry Pi or a Jetson) and on macOS (Apple silicon and Intel), reading only what needs no root:

```text
  This computer
    Computer                MacBook Pro, arm64
    System                  macOS 26.6
    Processor               Apple M4 Pro, 12 cores (8 performance, 4 efficiency)
    Memory                  24 GB LPDDR5
    GPU                     Apple M4 Pro, 16 cores
    Disk                    500.3 GB SSD (Apple Fabric), 131.3 GB free, writes 6.6 GB/s
    Network                 Ethernet, 10 Gb/s
```

It shows each disk with its size, kind (SSD, NVMe SSD, HDD or SD card), bus and the system disk's free space, and
each network link that is up with its speed. The write speed comes from a short test: 256 MB written to the state
folder, flushed and removed (`--no-benchmark` skips it).

Setup then asks whether to share these details with the lab. With `share_hardware = true` in `worker.toml`, the
worker sends them with every report and `labrat worker show <name>` lists them. Nothing in them identifies the
computer: no serial numbers, MAC addresses, user names or host names.

## What a worker reports

Every request for work carries the computer's state: CPUs, memory, GPUs (from `nvidia-smi`, with whether
something runs on each), load, whether anyone is logged in, whether it is taking work now, its limits, and which
of the lab's stores it can reach with their free space. Everyone sees these. Its host name, platform, Python and
LABRAT versions, addresses and link address are shown to its owner and to admins only. `labrat who` lists the
workers with the devices.

## Jobs

A job is a script, its arguments and what it needs. The nest queues it and starts it on workers that take the
person's jobs, are taking work now, have a free slot and fit what it needs.

```bash
labrat job run fit.py model.py --gpus 1               # fit.py runs; model.py travels with it
labrat job run train.py --nodes 2 --exclusive         # two workers at once, to itself
labrat job run sweep.py --on gpu-box -- --steps 40    # what follows -- goes to the script
labrat job run known.py --installed                   # a script already in the worker's scripts folder
labrat jobs                                           # yours, newest first
labrat job output 12 -f                               # what rank 0 prints, as it prints it
labrat job show 12                                    # every part, its worker, state and exit code
labrat job stop 12
```

| Option | Meaning |
| --- | --- |
| `--on` | these workers, comma-separated, in this order |
| `--nodes` | how many workers at once (by default one, or as many as `--on` names) |
| `--cpus`, `--memory`, `--gpus`, `--gpu-memory` | what each part needs; `--memory` and `--gpu-memory` in GB |
| `--exclusive` | wait until the workers run nothing else and their GPUs are free |
| `--device` | a device the job may change (repeat it for more) |
| `--env NAME=VALUE` | an environment variable for the script |
| `--minutes` | the time limit, after which the job is stopped |
| `--name` | a name for the job (by default the script's) |
| `--store PURPOSE=STORE[:PATH]` | where one kind of result goes ([below](#where-results-go)) |
| `--output STORE:PATH` | where what the job prints goes |

A `.py` script runs with the worker's Python, a `.sh` script with `/bin/sh`. At most 16 MB of files travel with a
job. From Python, `Client().run_job("fit.py", ["model.py"], ["--steps", "40"], gpus=1)` does the same, and an AI
agent has `labrat_job` and `labrat_workers` ([MCP](mcp.md)).

### Where it runs

Without `--on`, the nest prefers the person's own workers, then idle ones, then the least loaded. A worker runs
as many jobs at once as its `slots`, and none while it is not taking work (`when`, `hours`). A worker that takes
only installed scripts (`accept = "scripts"`) is never given a script that travels with a job. The worker checks
its owner's rules again before it runs anything, whatever the nest says.

### Several workers at once

A job with `--nodes 2` or more starts only when that many workers are free, and every part starts at the same
time. Each part gets its place in its environment, under LABRAT's names and the ones `torch.distributed` and
similar libraries read:

| Variable | Meaning |
| --- | --- |
| `LABRAT_RANK`, `RANK` | this part, from 0 |
| `LABRAT_WORLD_SIZE`, `WORLD_SIZE` | how many parts |
| `LABRAT_LEADER`, `MASTER_ADDR` | rank 0's address: its `link` address when it has one, so the parts talk over the fast link |
| `MASTER_PORT` | rank 0's `link_port` (29500) |
| `LABRAT_JOB`, `LABRAT_JOB_NAME`, `LABRAT_WORKER` | the job, its name and the worker |

The parts talk to each other directly, at whatever speed the link between the workers gives; none of that
traffic passes through the nest. When one part fails, the nest stops the others.

### Inside a job

```python
from labrat import job

lab = job.client()                           # the nest, with this part's own token
if job.rank == 0:
    lab.note(f"fitting on {job.world_size} workers")
```

A job that drives a device on a rat, such as a display fed from a GPU, reaches it directly:
`job.client().direct("optics-rat/slm")` takes a lease for one of the job's devices and talks to the rat on its
fastest address, without the nest in between ([clients](clients.md#straight-to-a-rat)).

Each part runs with a token of its own, which the nest and the worker both derive from the worker's key, so it
never travels. It acts as the person who sent the job, with their limits, locks and approval rules, but changes
only the devices the job names with `--device`. It cannot start other jobs, recordings, people, workers or
approvals, and it stops working when the part ends. The audit log records the person with the job's number.

### Where results go

A job writes its results straight to the lab's stores, through the worker's own mounts: a NAS over NFS or SMB,
a cluster's scratch space, a local disk. None of the data passes through the nest, so a job writes as fast as the
link between the worker and the store allows. Each kind of result can go to a different store:

```toml
# nest.toml
[jobs]
stores = { data = "nas", raw = "nas-raw" }     # purpose = store; data is the default purpose
path = "%u/%x-%j"                              # each job's folder on a store (the default)

[jobs.people.ada]
stores = { raw = "ada-scratch" }               # ada's raw data goes elsewhere
```

```toml
# worker.toml: where those stores are on this computer
[stores.nas]
path = "/mnt/labnas/labrat"
mount = "/mnt/labnas"                          # only while this is mounted
[stores.nas-raw]
path = "/mnt/raw/labrat"
```

```python
from labrat import job
(job.path("raw") / "frames.bin").write_bytes(frames)     # /mnt/raw/labrat/ada/sweep-12/frames.bin
(job.path() / "fit.csv").write_text(table)               # the data folder
```

A job chooses for itself, like Slurm's `--output`, with the same codes in paths: `%j` the job's number, `%x` its
name, `%u` the person, `%N` the worker, `%t` the rank, `%%` a percent sign.

```bash
labrat job run sweep.py --store raw=nas-raw:%u/runs/%x/%j --output nas:logs/%x-%j.out
```

The job's choice comes first, then the person's (`[jobs.people.<person>]`), then the lab's (`[jobs]`), and a lab
with experiments uses the experiments' store for `data` when `[jobs]` names none. A job started while its
person has an experiment open writes its `data` into that experiment's folder.

- What the job prints goes to `output.txt` in its data folder (`output-<rank>.txt` for each part of a group), or
  to the file `--output` names, as well as to `labrat job output`.
- When the job ends, the worker lists every file it wrote with its size (and SHA-256 with `hash = true`), sends
  the list to the nest, where `labrat job show` displays it, and writes `job-<number>.json` beside the results.
- Paths stay inside their store: absolute paths and `..` are refused, and the worker checks where each path
  really leads. A folder that already holds another job's files is never written into, and an output file is
  never written over. Keep `%j` in the path, as the default does.
- A job goes only to workers that have every store it needs, mounted.
- On a cluster whose compute nodes cannot see the NAS, point the store at scratch space and let the worker copy
  each job's results on when it ends: `copy = "rsync -a {src}/ nas-host:/export/labrat/{rel}/"`, where `{src}` is
  the job's folder on this computer and `{rel}` its path inside the store. A failed copy marks the job failed.
- Store credentials stay in the computer's own mount set-up (`/etc/fstab`, autofs, a Kerberos keytab). The nest
  never holds or sends them, and never learns where a store is mounted.

### Stopping

`labrat job stop` sends each part Ctrl-C (SIGINT), so a script can clean up and leave its devices safe, then
SIGTERM after 10 s and SIGKILL after 20 s. The time limit stops a job the same way. The nest keeps every job's
record in `<state_dir>/jobs/`, and a job that was queued or running when the nest stopped is marked failed when
it starts again.

### Agents

An AI agent's token starts jobs like its person's, as long as no approval rule says otherwise. To have its jobs
wait for a person:

```toml
[[approvals]]
who = ["claude-*"]
ops = ["job"]
```

## Clusters

A worker on a cluster's login node hands each job to the cluster's scheduler instead of running it itself. It runs
under your own account there, so the cluster's own rules and accounting apply, and the nest never logs in
anywhere.

```toml
backend = "slurm"             # that is all Slurm needs; everything below is optional

[slurm]
partition = "gpu"
account = "my-group"
extra = ["--qos=normal"]                          # more sbatch options
setup = "module load cuda/12.4"                   # lines run before the job
container = "apptainer exec --nv ~/labrat.sif"    # run the script inside a container
template = "~/labrat-job.sbatch"                  # a batch script of your own in place of LABRAT's
gres = "gpu:{gpus}"                               # how GPUs are asked for
export = "NONE"                                   # sbatch --export
workdir = "/scratch/labrat"                       # where each job's folder goes, seen by the compute nodes
keep = true                                       # keep each job's folder after it ends
poll_s = 10                                       # how often the job's state is checked

[slurm.commands]                                  # each one can be replaced, for a wrapper or another path
submit = "sbatch --parsable {script}"
status = "squeue -h -j {id} -o %T"
accounting = "sacct -n -P -X -j {id} -o State,ExitCode"
interrupt = "scancel --signal=INT --full {id}"
cancel = "scancel {id}"
```

For each job the worker writes the job's files and a batch script into a folder of its own under `workdir`,
submits it, follows it with `status` until it ends, takes the exit code from `accounting`, and passes on what the
job writes to its output as it appears. What the job needs becomes sbatch options: `--cpus` becomes
`--cpus-per-task`, `--memory` becomes `--mem`, `--gpus` becomes `--gres`, `--minutes` becomes `--time` and
`--exclusive` stays `--exclusive`. Stop sends `interrupt` (Ctrl-C to every task) and, 10 s later, `cancel`.

A job for several workers (`--nodes 4`) that goes to a cluster becomes one allocation of four nodes with one
task on each. `srun` starts the tasks, each with its own `RANK`, and the first node is `MASTER_ADDR`, so the same
script runs on two workstations with a fast link or on four cluster nodes with InfiniBand. The parts talk over the
cluster's own network; nothing between them passes through the worker or the nest.

The job's variables, its token among them, go into `job.env` in its folder, readable by your account only, and
never onto a command line or into the batch script. `PATH`, the modules and the GPUs come from the compute node,
your login profile (the script starts with `#!/bin/bash -l`) and `setup`. Set `python` in `worker.toml` to a
Python the compute nodes have, or use `container`.

In a template of your own, `{job_name}`, `{workdir}`, `{nodes}`, `{options}` (the `#SBATCH` lines),
`{setup}`, `{container}`, `{command}`, `{cpus}`, `{gpus}`, `{minutes}` and `{job}` are filled in, and anything
else, such as `${HOME}`, is left alone. Another scheduler works the same way with `backend = "batch"`, a
`template`, and `submit`, `status`, `accounting` and `cancel` under `[batch.commands]`; `accounting` must print
`STATE|EXIT` (such as `COMPLETED|0`).

The nest does not check a cluster's CPUs and GPUs: the scheduler decides what fits. Compute nodes often cannot
reach the lab network. A job that needs the nest (to read a device, write a note) needs nodes that can reach it;
one that only computes does not.

## worker.toml

`labrat worker` reads `worker.toml` from `--config`, `/etc/labrat/` or `~/.config/labrat/`. Unknown settings are an
error. Every setting is optional except `nest`, which `labrat worker join --url` can supply instead.

| Setting | Default | Meaning |
| --- | --- | --- |
| `nest` | | the nest's address |
| `name` | the host name | how the worker is known |
| `state_dir` | as for a nest or rat | where its key (`worker.key`) and what it joined (`worker.json`) are kept |
| `shared` | `false` | offer it to other people as well, once an admin agrees |
| `people` | `["*"]` | whose jobs it takes when shared, as names or shell-style patterns; its owner's always |
| `accept` | `"any"` | `"scripts"` runs only scripts already in the `scripts` folder |
| `scripts` | | the folder for `accept = "scripts"` |
| `when` | `"always"` | `"idle"` takes work only while nobody is logged in |
| `hours` | any time | `"18:00-07:00"` takes work only in these hours |
| `cpus`, `gpus`, `memory_gb` | the whole computer | the most a job may use |
| `backend` | `"local"` | how jobs run: `"local"`, `"slurm"` or `"batch"` |
| `slots` | `1` local, `32` otherwise | how many jobs at once |
| `link`, `link_port` | none, `29500` | this computer's address and port on a fast link, for jobs across several workers |
| `python` | the one LABRAT runs on | the Python that runs job scripts |
| `pass_env` | none | environment variables a job inherits, such as `["CUDA_HOME"]` |
| `allow_http` | `false` | allow plain HTTP to a nest on a public address |
| `hash` | `false` | add a SHA-256 of every output file to the job's record |
| `share_hardware` | `false` | send the computer's model, processor, memory, GPUs, disks and network links with its reports |
| `[stores.<name>]` | none | where the lab's stores are on this computer: `path`, and optionally `mount` and `copy` |

## Security

- **Only outgoing connections.** The worker asks the nest for work. Nothing on the computer listens, so a worker
  behind NAT, a laptop on Wi-Fi or a cluster login node needs no open port.
- **Signed requests and replies.** The worker makes a key when it first starts (`worker.key`, readable by its
  owner only) and sends it to the nest once, when it joins with its owner's token. After that, every request
  carries an HMAC of the time, a fresh nonce and the body, and every reply an HMAC of that nonce and the reply.
  The key does not travel again, a recorded request is refused when sent a second time, and the worker refuses
  any reply its nest did not sign.
- **Private networks for plain HTTP.** A worker uses plain HTTP only to a nest on a loopback, private,
  link-local or carrier-grade NAT address (VPNs such as Tailscale use these) or a `.local` name. Signing keeps
  requests from being forged or changed, but not from being read: for anything beyond the lab network, put the
  nest behind TLS or reach it over a VPN.
- **The owner's settings stay with the owner.** Whose jobs, when, how much and which code are set in
  `worker.toml` on the computer. The nest cannot change them, and the worker checks them again for every job.
- **A clean environment.** A job sees `PATH`, `HOME`, `USER`, the locale, a temporary folder of its own, the
  variables the owner names in `pass_env`, its own `--env` and LABRAT's. Nothing else from the worker's
  environment, such as the owner's API keys, reaches it. Run the worker as a user of its own (`labrat service
  worker`) so a job cannot read its owner's files either.
- **Tokens that end with the job.** A part's token is derived on both sides, never sent, limited to the
  job's devices and removed when the part ends.
- **Private details stay private.** Host names, addresses and platforms are shown to the worker's owner and to
  admins, never to other people or their agents.
