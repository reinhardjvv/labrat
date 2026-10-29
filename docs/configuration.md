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
| `[[tokens]]` | none | who may connect, below |
| `[devices.<name>]` | none | the devices served, below |

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

`limits` narrows an attribute's range and can never widen it. `safe` names the value each attribute takes in
the safe state. It applies to every channel of a per-channel attribute.

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
