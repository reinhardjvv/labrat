# Configuration

A nest reads `nest.toml` and a rat reads `rat.toml`, both in [TOML](https://toml.io). Without `--config`, each
looks in `/etc/labrat/` and then `~/.config/labrat/`. `labrat nest init` and `labrat rat init` write a starting
file. Unknown settings are an error, so a typing mistake stops the server with a message saying which.

## Common settings

| Setting | Default | Meaning |
| --- | --- | --- |
| `name` | the host name (`<host>-nest` for a nest) | how this nest or rat is known, in letters, digits, `-` and `_` |
| `host` | `"0.0.0.0"` | the address to listen on, `"127.0.0.1"` keeps it to this computer |
| `port` | `8780` nest, `8781` rat | the HTTP port |
| `state_dir` | `/var/lib/labrat` under systemd or root, else `~/.local/state/labrat` | keys, adopted rats and the audit log |
| `discovery` | `true` | announce on the network and, for a nest, look for rats |
| `lock_minutes` | `10` | how long a lock lasts without use |
| `[[approvals]]`, `approval_minutes` | none, `30` | requests that wait for a person, below |
| `[[tokens]]` | none | who may connect, below |
| `[devices.<name>]` | none | the devices served, below |
| `[network]` | none | a private network for the instruments and rats ([network](network.md)) |

## Nest settings

| Setting | Default | Meaning |
| --- | --- | --- |
| `heartbeat_s` | `5` | how often the nest checks each adopted rat |
| `[limits."<device>".<attribute>]` | none | the nest's own limits for any device, its own or a rat's |

A nest that listens on the network refuses to start without at least one token.

## Rat settings

| Setting | Default | Meaning |
| --- | --- | --- |
| `watchdog_s` | `30` | seconds without any request, after an output changed, before the safe state (`0` turns it off) |

## Tokens

```toml
[[tokens]]
name = "ada"
role = "admin"            # viewer, operator or admin
sha256 = "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08"
```

Only the SHA-256 of a token is stored. `labrat token --name ada --role admin` makes a token and prints the
entry. Give every person and every program, AI agents included, a token of its own: the audit log records
the name. A rat accepts its own key as well, under the name `nest`.

A token may act for a person with `for = "<person>"`. An AI agent's token for ada then works in ada's
[experiments](experiments.md), and the logs name both the token and ada. Such a token can never approve a request.

### Users added while the nest runs

An admin adds and removes people without touching the file or restarting the nest:

```bash
labrat user add aatikah --role operator          # prints her token once
labrat user add claude-reinhard --for reinhard   # an AI agent working for reinhard
labrat users                                     # everyone, their role, who added them, when last active
labrat user remove aatikah                       # her token stops working, her locks are released
```

These users live in `<state_dir>/users.json` (readable by the nest's owner only, tokens stored as SHA-256).
Tokens in `nest.toml` keep working beside them and are changed in the file. God mode is set in the file only.

`god_mode = true` lifts the lab's limits and approval rules for one operator or admin token, and marks its
requests in the audit log. Locks, the instruments' own ranges and `stop` still apply. A rat passes it on only
with `allow_god_mode = true` in `rat.toml` ([MCP](mcp.md#god-mode)).

## Devices

```toml
[devices.psu]
driver = "scpi"                      # labrat drivers lists what this installation has
description = "bench supply on the optics table"
# driver settings follow, see drivers.md

[devices.psu.limits.VoltageLevel]
min = 0
max = 5                              # tighter than the instrument's own range

[devices.psu.safe]
OutputEnabled = false                # written on stop, on shutdown and by the watchdog
```

Every device may also carry its identity: `kind`, `manufacturer`, `model`, `serial`, `firmware`, `location`
and `tags` ([naming.md](naming.md)). Instruments that answer `*IDN?` fill in the rest themselves.

`limits` narrows an attribute's range and can never widen it. `safe` names the value each attribute takes in
the safe state. It applies to every channel of a per-channel attribute.

## Stores and recordings

`[stores.<name>]` and `[recordings.<name>]` write readings, measurements and streams to storage on a schedule.
See [recordings.md](recordings.md).

## Experiments

`[experiments]` with `store = "<store>"` gives every person a folder of experiments on that store
(nest only). See [experiments.md](experiments.md).

## Approvals

```toml
approval_minutes = 30              # a request nobody decides on expires after this long

[[approvals]]
who = ["claude", "student-*"]      # token names, shell-style patterns, "*" for everyone
ops = ["set", "call"]              # set, call and lock
devices = ["*/laser*", "psu"]      # device ids
names = ["OutputEnabled"]          # attribute or function names
```

A request that matches any rule waits for a person: it answers with the error `pending` and an approval id.
Someone with an operator or admin token, other than the one who asked, approves it with `labrat approve <id>`
or denies it with `labrat deny <id>`. It then runs with the rights, limits and locks of the one who asked. A
`stop` denies everything waiting. Rules work the same way in `rat.toml`. Without rules nothing waits.

## Requiring a lock

```toml
require_lock = ["bench-rat/*", "psu"]            # or true for every device
```

On these devices a change (`set` or `call`) is refused until the sender holds the lock, so taking control is
the rule and not a courtesy. Reading needs no lock. Recordings that call functions run without one, though a
lock someone else holds still stops them (nest only).

## Limits at the nest

```toml
[limits."bench-rat/psu".VoltageLevel]
max = 3.3
```

The nest checks these before a change leaves it. The rat checks its own limits again, so the tighter of the
two always holds.

## A complete rat

```toml
name = "optics-rat"
watchdog_s = 20

[devices.shutter]
driver = "gpio"
chip = "/dev/gpiochip0"
outputs = { shutter = 17, laser_enable = 27 }
inputs = { door_closed = 22 }
safe = { shutter = false, laser_enable = false }

[devices.slm]
driver = "display"
framebuffer = "/dev/fb0"

[devices.cam]
driver = "genicam"
[devices.cam.limits.ExposureTime]
max = 0.05
```

More examples are in [examples/](../examples).
