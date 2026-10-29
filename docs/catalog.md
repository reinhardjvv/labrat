# Device catalog and reverse engineering

A profile describes one model: how to recognise it, how it talks, and the real replies it was worked out
from. A lab with a profiled device writes two lines. An unknown device becomes a profile through scanning,
probing and capturing, with a person or an AI agent doing the detective work.

```toml
[devices.ups]
profile = "mecer/me-2000-wpru"
port = "hid:///dev/hidraw0"
```

`labrat profiles` lists the catalog. LABRAT comes with a few profiles and reads more from the folders in
`catalog` (by default `<state_dir>/profiles`).

## A profile

```toml
[profile]
manufacturer = "mecer"
model = "ME-2000-WPRU"
kind = "ups"
description = "2000 VA on-line UPS speaking the Voltronic variant of the Megatec protocol"
evidence = "replies recorded from a unit with firmware 01833.01 on 20 Oct 2026"
status = "tested"                     # or draft

[[match]]                             # any one recognises the model
usb = "0665:5161"
probe = "QMD"                         # a read-only query, and what its reply contains
reply = 'WPHVR2K0'

[device]                              # a [devices.<name>] table, without the port
driver = "serial"
line_ending = "cr"

[device.attributes.BatteryCharge]
get = "QBV"
reply = '^\((?:\S+ ){3}(\S+)'
unit = "%"

[[recording]]                         # verbatim exchanges with a real unit
send = "QBV"
reply = "(054.6 04 01 094 306"
```

A SCPI instrument is matched by its `*IDN?` reply instead, with a pattern such as
`idn = "TEKTRONIX,MSO54,*"`. The lab's own table wins over the profile, attribute by attribute, so limits, a location or a changed timeout
go next to `profile`. Binary devices record `send_hex` and `reply_hex`.

The recordings are evidence, a simulator and tests at once. `simulate = true` makes a device answer from them,
for demos, for tests and for planning a lab before its hardware arrives. The test suite reads every attribute
of every built-in profile from its recordings, so a profile that stops matching its own evidence fails.

## Finding devices

```bash
labrat scan                        # on the nest
labrat scan --rat bench-rat        # on a rat
labrat scan --rat bench-rat --probe
```

A scan lists USB devices with their vendor and product IDs and device files, serial ports, USBTMC instruments,
GPIO chips and I2C buses, with the profiles that fit. Listing sends nothing. `--probe` sends each fitting
profile's own read-only match query to confirm the model. A rat also writes the fits to its log when it starts.

## Probing an unknown device

```bash
labrat probe /dev/ttyUSB0 --rat bench-rat
labrat probe hid:///dev/hidraw0 --protocol megatec
labrat probe tcp://192.0.2.50:5025
labrat probe http://192.0.2.30
labrat probe i2c:1
labrat probe /dev/ttyUSB0 --baud 9600 --line-ending cr --send "VER?" --send "RAT?"
```

On a serial port with no settings, the probe tries the common baud rates and line endings, with SCPI, the
Megatec UPS protocol and plain text, until something answers in readable text. It then asks the rest of that
protocol's status queries. A web address gets `GET` requests for the common status paths of web-controlled
devices. An I2C bus gets each address read once, with the chips that commonly sit at the ones that answer.

Every exchange goes into a transcript in `<state_dir>/probes`, and the profiles whose match the replies fit
are named.

**Probing is read-only.**
- Probes send queries ending in `?` and known status queries.
- Anything else, such as a guess at a setting command, waits for a person to approve it. That holds even for
  tokens in god mode, as a wrong guess can damage hardware.
- Words that reset, reboot, shut down, test, calibrate, erase or update a device are never sent. Neither are
  the Megatec commands that switch an output off or change a setting. A command like that belongs in a profile
  as a function, where limits and approvals apply.
- A port already serving a device is refused.

## Capturing the vendor's software

When a device only talks to its maker's program, record the program talking to it:

```bash
labrat capture start serial /dev/ttyUSB0 --baud 9600 --rat bench-rat
#   point the vendor software at the printed path, use it, then:
labrat capture stop
labrat capture start usb 0665:5161      # usbmon: nothing sits in between
labrat capture stop
```

A serial capture opens the real port and a pseudo-terminal, passes every byte between them, and keeps both
directions. A USB capture reads the kernel's USB monitor for that device. It needs `sudo modprobe usbmon` and
read access to `/sys/kernel/debug/usb/usbmon`, which usually means running that rat as root while capturing.
Both end as transcripts in `<state_dir>/captures`, in the same form as probes.

## From a draft to a device

```bash
labrat profile try draft.toml --port /dev/ttyUSB0 --rat bench-rat
labrat profile save draft.toml --transcript 2026-10-30/091502-dev-ttyUSB0.json --rat bench-rat
labrat add bench-rat/pump --profile acme/pump-1 --port /dev/ttyUSB0
```

`try` builds the device on the real port, reads every attribute on every channel, changes nothing, and
reports what read and what did not. `save` puts the profile in the catalog. The replies in the named probes and
captures become its recordings, so the evidence travels with it. Operators save drafts. An admin marks a profile
`tested`.

`labrat add` makes the device live at once, on the nest or on the rat in its name. It survives restarts and
`labrat remove` takes it away again. Adding and removing need an admin. A device added this way is like any
other: limits, locks, approvals, recordings, experiments and MCP all apply.

## With an AI agent

An agent connected over MCP has `labrat_scan`, `labrat_probe`, `labrat_capture`, `labrat_profiles`,
`labrat_try_profile` and `labrat_save_profile`. Ask it:

```text
Something new is plugged into /dev/ttyUSB0 on the bench rat. Work out what it is, make a draft profile,
and show me the evidence for each value.
```

It scans, probes, writes a draft, tries it until every attribute reads, and saves it with its transcripts.
Anything beyond a query waits for you, and a person adds the device.

## Sharing a profile

Profiles are plain files. To add one to LABRAT, put it in `src/labrat/profiles/<manufacturer>/<model>.toml`
with recordings from a real unit, say in `evidence` where they came from, and open a pull request. Leave out
serial numbers and anything else that identifies your unit.
