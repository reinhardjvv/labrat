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
- Third-party drivers through the `labrat.drivers` entry point group.
- The Python client, the MCP server for AI agents, the `labrat` command and `labrat demo`.
- An install script and systemd units for nests and rats.
- Adoption and address changes that send a rat's key only after the rat proves it holds it (HMAC challenge).

[Unreleased]: https://github.com/reinhardjvv/labrat/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/reinhardjvv/labrat/releases/tag/v0.1.0
