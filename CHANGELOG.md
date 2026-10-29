# Changelog

All notable changes to LABRAT are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and LABRAT uses
[semantic versioning](https://semver.org).

## [Unreleased]

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

[Unreleased]: https://github.com/reinhardjvv/labrat/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/reinhardjvv/labrat/releases/tag/v0.1.0
