# Changelog

All notable changes to LABRAT are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and LABRAT uses
[semantic versioning](https://semver.org).

## [Unreleased]

## [0.3.0] - 2026-11-01

### Added

- `labrat network`: a private network of the nest's or a rat's own, on a spare Ethernet port or a Wi-Fi access
  point, with fixed addresses by MAC address and, by default, no route beyond the computer. NetworkManager runs
  it, and `plan` shows every change before `up` applies it.
- Workers: computers that run jobs for a nest. `labrat worker join` adds one as its owner's, `--shared` offers it
  to others once an admin agrees, and `labrat workers` lists each with its CPUs, memory, GPUs and stores. Workers
  connect out to the nest with HMAC-signed requests and refuse unsigned replies; host details are shown to their
  owners and admins only.
- Jobs: `labrat job run` sends a script to the workers, queued until workers that fit are free, with CPUs,
  memory, GPUs, `--nodes` for a job across several workers started together, `--exclusive`, `--device` and a
  time limit. Each part runs with a clean environment and a token derived on both sides that acts as the person
  but changes only the job's devices. `[[approvals]]` can make jobs wait with `ops = ["job"]`. MCP tools
  `labrat_job` and `labrat_workers`.
- Clusters: `backend = "slurm"` in `worker.toml` hands each job to Slurm from the login node, with partition,
  account, extra options, setup lines, a container, a template and every command configurable. A job for several
  nodes becomes one allocation with `srun` giving each task its rank. `backend = "batch"` drives another
  scheduler from a template and four commands.
- Job results go straight from the worker to the lab's stores through its own mounts. `[jobs]` and
  `[jobs.people.<person>]` give each purpose (`data`, `raw`, ...) a store, `--store` and `--output` choose per
  job with Slurm's `%j %x %u %N %t`, and `labrat.job.path(purpose)` finds the folder. Workers list every file
  they wrote for the nest, write `job-<n>.json` beside the results, never write into another job's folder, and
  can copy results on with a store's `copy` command.
- Leases and direct paths: `Client.direct("rat/device")` takes a lease from the nest and talks to the rat on its
  fastest address that proves it is the real rat, renewing in the background and falling back to the nest. The
  lease carries the nest's limits and is signed with the rat's key; the rat refuses everyone else's changes,
  ends leases on a stop, and puts the devices in their safe state when a lease that changed something runs out.
  No lease is granted where approval rules apply, and a job's token gets one only for its own devices.
- Fast changes through a lease are logged as the first in full and then a count with a running SHA-256 every
  10 s; `log = "every"` on a device logs each one.
- A `bytes` argument travels as the raw body of the request (`application/octet-stream`), and the display
  driver takes frames that way. The Python client keeps each thread's connection open.
- The audit log keeps long values, such as frames sent to a display, as their size and SHA-256.

## [0.2.0] - 2026-10-30

### Added

- Device catalog: profiles that recognise a model by USB ID, `*IDN?` or a probe, describe it for an existing
  driver, and keep the real replies it was worked out from. `profile = "..."` uses one, and `simulate = true`
  answers from its recordings. Profiles for the Mecer ME-2000-WPRU UPS and the Tektronix AFG3252 and MSO54.
- `labrat scan`, `labrat probe` (read-only by default, with a deny list and approval for anything else) and
  `labrat capture` (serial pass-through and usbmon) for unknown devices, and `labrat profile try` and `save`.
- `labrat add` and `labrat remove`: devices added while the nest or a rat runs, from a profile or a driver.
- The `frames` driver for Modbus RTU and TCP and fixed binary frames with checksums, and USB HID bridges in the
  `serial` driver.
- MCP tools for the workbench, so an agent can work out a device and draft its profile.

## [0.1.0] - 2026-10-29

First release of LABRAT as a nest, rats and one message API.

### Added

- The LABRAT message: `health`, `list`, `describe`, `get`, `set`, `call`, `lock`, `unlock`, `stop`, `rats`,
  `adopt` and `forget`, over `POST /api/v1/message`, single or batched.
- The nest: tokens with viewer, operator and admin roles, limits for any device, locks, an audit log, and
  `stop` forwarded to every rat.
- Rats: the same API at the bench, a key made on first start, multicast DNS announcement with a key
  fingerprint, and a watchdog that applies the safe state when the nest falls silent.
- Discovery (`labrat find`), adoption, heartbeats, and following a rat to a new address.
- Streams: `GET /api/v1/streams/<device>/<stream>`, passed through the nest from rats.
- Drivers: `scpi` (TCP, serial and USBTMC), `gpio`, `camera` (USB, CSI, RTSP and MJPEG), `genicam` (GigE Vision
  and USB3 Vision), `display` (HDMI SLMs and DMDs), and the simulated `sim-supply` and `sim-gpio`.
- Drivers for the rest of the lab: `serial` (text commands with their own port settings and reply parsing),
  `i2c` (registers, bit fields and scaling), `mqtt`, `http` and `nut` (UPSs through Network UPS Tools).
- Third-party drivers through the `labrat.drivers` entry point group.
- Identities: kind, manufacturer, model, serial, firmware, location and tags for every device, from `*IDN?` or
  the configuration, a reference per physical instrument, standard kinds with aliases, and Spack-style
  selectors to find, sort and group devices (`labrat devices oscilloscope tektronix/* --by kind`,
  `Client.device(...)`).
- Recordings: attributes, functions and streams written to stores on a schedule, as CSV or JSON lines, with
  timestamps and a metadata file per run.
- Stores on local disks or network shares, with an optional mount check and a local buffer that copies to the
  share when it can be reached.
- Experiments: each person's work in their own folder, with its commands, instrument snapshots at start and
  stop, notes and recordings. `files` and `GET /api/v1/files/...` read them back, and a token may act for a
  person (`for`), so an AI agent works in that person's folder.
- Users added and removed while the nest runs (`labrat user add`, `labrat users`), `labrat who` for who holds
  and last changed each device, and `require_lock` to make a lock necessary before any change.
- God mode for chosen tokens: no lab limits or approval rules, with locks, instrument ranges, `stop` and the
  audit log kept, and rats that keep their own limits unless they allow it.
- Approvals: rules that make chosen requests wait for a person, with `approvals`, `approval`, `approve` and `deny`.
- Binary blocks and number lists in the `scpi` driver, for waveforms and traces from scopes and analysers.
- The Python client, the MCP server for AI agents, and `labrat demo`.
- The `labrat` command: coloured tables, `describe` with live readings, `watch`, `stream`, and an interactive
  `shell` with tab completion.
- An install script and systemd units for nests and rats.
- Adoption and address changes that send a rat's key only after the rat proves it holds it (HMAC challenge).

[Unreleased]: https://github.com/reinhardjvv/labrat/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/reinhardjvv/labrat/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/reinhardjvv/labrat/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/reinhardjvv/labrat/releases/tag/v0.1.0
