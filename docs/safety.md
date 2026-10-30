# Safety and security

LABRAT's first job is to stop people, scripts and AI agents from damaging equipment.

## Limits

Every attribute can carry a range, and every write is checked against it before anything reaches the
device. Ranges come from three places, and each can only narrow the one before it.

1. The driver, from the equipment's own range or absolute maximum ratings.
2. The device's `limits` in the rat's or nest's configuration, for what is connected to it.
3. The nest's `[limits]`, for rat devices, checked before a change leaves the nest.

A value outside the range is refused with the error `limit`, and nothing is sent. Booleans, integers and
numbers are converted strictly: not a number, infinity and ambiguous strings are refused.

## Safe states

Each device names a safe state: outputs off, a shutter closed, an SLM blank. It is applied

- on `stop`, which any role may send, and which the nest forwards to every rat,
- when a nest or rat shuts down, to its own devices (a nest that restarts leaves its rats alone, and their
  watchdogs act if it stays away),
- by a rat's watchdog, when it has changed an output and then hears nothing from its nest or any client for
  `watchdog_s` seconds.

Applying the safe state continues past a failing device, so one broken instrument cannot keep the others on,
and the reply lists every problem met. The nest stops its own devices and every rat at the same time. A rat
that restarts treats its outputs as possibly on, so its watchdog acts if no request comes. A device that can
be changed but has no safe state is reported in the log when its nest or rat starts.

A token in god mode skips the lab's limits but not the instrument's own range ([MCP](mcp.md#god-mode)).

## Locks

A lock gives one name a device for a set time. Others are refused with `locked`. Use refreshes a lock for the
time its holder chose, an admin can release anyone's, and a `stop` leaves locks with their holders. Locks
are enforced for changes. Anyone with a token can still read a locked device. `require_lock` makes a lock
necessary before any change ([configuration](configuration.md#requiring-a-lock)), and `labrat who` shows who
holds each device, who changed it last and how, and when each person was last active.

## Approvals

Rules in the configuration make chosen requests wait for a person, by who sends them, the operation, the
device and the name ([configuration.md](configuration.md#approvals)). Nobody can approve their own request,
viewers cannot approve, and a `stop` denies everything waiting. How many guardrails a lab uses is the lab's
choice: none of them is required.

## Raw commands

Raw instrument commands cannot be checked against limits, so the `scpi` driver turns them off unless the
configuration says `raw = "query"` (commands ending in `?` only) or `raw = "on"`. Even queries can change an
instrument: `READ?` switches some source meters' outputs on, and `*TST?` runs a self-test. Describe the
commands you need as functions and leave raw off where you can. A value or argument that
contains a newline or `;` is refused, so it cannot carry a second command.

## Tokens and roles

Every request but `health` needs a bearer token. Only the SHA-256 of each token is stored. Roles are viewer,
operator and admin. Give each person and each program its own token, as the audit log
(`<state_dir>/audit.jsonl`) records names. A rat's key is kept in `<state_dir>/rat.key` and the nest's copy of
each key in `<state_dir>/rats.json`, both readable by their owner only. Keys never enter the audit log.

## The network

LABRAT speaks plain HTTP. Keep nests and rats on a lab or instrument network, never on the open internet.
To reach a nest from elsewhere, put it behind a reverse proxy with TLS, such as Caddy or nginx, or reach the
network over a VPN such as WireGuard or Tailscale. `labrat network` gives a nest or rat a private network of
its own for its instruments, which by default reach nothing beyond it ([network](network.md)).

Multicast DNS only finds things. Adopting a rat needs its key, and the nest sends that key only after the rat
proves it holds it: the nest sends a random challenge and checks the rat's HMAC of it. The nest follows a rat to
a new address in the same way, so a host that copies a rat's name and fingerprint learns nothing. Keys are
asked for by `labrat adopt`, never given on its command line, so they stay out of shell history.

The systemd units run LABRAT as an unprivileged `labrat` user with a read-only system, a private `/tmp` and
no access to home directories.

## Reporting a vulnerability

See [SECURITY.md](../SECURITY.md).
