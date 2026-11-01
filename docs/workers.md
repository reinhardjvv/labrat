# Workers

A worker is a computer that runs jobs for a nest: a GPU workstation, a laptop, a compute box with a fast link to
another one, or the login node of a cluster. It connects out to its nest and asks for work, so nothing on it
listens for connections, and its owner decides in `worker.toml` what it takes.

## Joining

On the computer, with LABRAT installed and your own token in `LABRAT_TOKEN`:

```bash
labrat worker init          # writes worker.toml with the nest's address
labrat worker join          # joins it as your worker
labrat worker               # runs it
```

`labrat service worker` prints a systemd unit that keeps it running.

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

## What a worker reports

Every request for work carries the computer's state: CPUs, memory, GPUs (from `nvidia-smi`, with whether
something runs on each), load, whether anyone is logged in, whether it is taking work now, its limits, and which
of the lab's stores it can reach with their free space. Everyone sees these. Its host name, platform, Python and
LABRAT versions, addresses and link address are shown to its owner and to admins only. `labrat who` lists the
workers with the devices.

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
| `[stores.<name>]` | none | where the lab's stores are on this computer |

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
  `worker.toml` on the computer. The nest cannot change them.
- **Private details stay private.** Host names, addresses and platforms are shown to the worker's owner and to
  admins, never to other people or their agents.
