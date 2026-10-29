# Architecture

LABRAT has three parts: a message, a server called the nest, and small computers called rats.

## The message

Everything a client can do is one of a dozen operations in a small JSON object ([api.md](api.md)). The same
message travels from a client to the nest, from the nest to a rat and from an AI agent through the MCP server.
A client written for one device works for every device, and a new transport, such as MQTT or a raw TCP line
protocol, only has to carry the same messages.

## Devices

A device is whatever a driver presents: attributes to read and write, functions to call, streams to watch and
a safe state. Attribute names follow the IVI Foundation's class specifications where one exists, such as
`VoltageLevel`, `CurrentLimit` and `OutputEnabled` for a DC power supply, so the same name means the same thing
on every instrument.

Every write goes through `Attr.check`, which converts the value to the attribute's type and refuses anything
outside its range. Ranges come from the driver and can be narrowed, never widened, by the configuration of the
rat and again by the nest.

## The nest

The nest is the server a lab talks to. It

- authenticates every request with a bearer token tied to a name and a role,
- serves its own devices and those of its adopted rats, the latter as `<rat>/<device>`,
- checks every change against its own limits before passing it to a rat,
- keeps locks, so two people or programs do not change the same device at once,
- writes an audit log of every change, lock and stop (`<state_dir>/audit.jsonl`),
- sends `stop` to every rat in parallel when anyone asks for it.

## Rats

A rat runs the same message API for the devices plugged into it. It needs nothing from the nest to work and
can be used on its own with its key, which suits a single bench.

A rat announces itself on the local network with multicast DNS (service `_labrat._tcp`). The announcement
carries its name, version and key fingerprint. The nest lists rats it can see, and a person adopts one with
its key. The fingerprint lets that person check they adopt the rat in front of them.

## Heartbeat and watchdog

The nest asks every adopted rat for its health every few seconds (`heartbeat_s`, 5 s by default). It marks
rats online or offline from the answers, and follows a rat to a new address when discovery reports one with
the same key fingerprint. Rats on DHCP therefore keep working when their address changes.

Each heartbeat also feeds the rat's watchdog, as does every other authenticated request. A rat that has
changed an output and then hears nothing from its nest or any client for `watchdog_s` seconds (30 by default)
puts every device in its safe state. A crashed nest, a cut cable or a
lost Wi-Fi link then leaves the equipment safe.

## Code map

| Module | Role |
| --- | --- |
| `labrat/message.py` | the message, operations, roles and error codes |
| `labrat/device.py` | `Device`, `Attr`, `Function`, `Stream` and the limit checks |
| `labrat/drivers/` | built-in drivers and the driver registry |
| `labrat/hub.py` | what nest and rat share: tokens, locks, stop, audit |
| `labrat/nest.py` | the nest, rat links and adoption |
| `labrat/rat.py` | the rat, its key and its watchdog |
| `labrat/http.py` | the HTTP server |
| `labrat/discovery.py` | multicast DNS announcement and browsing |
| `labrat/client.py` | the Python client |
| `labrat/mcp.py` | the MCP server |
| `labrat/record.py` | stores and recordings |
| `labrat/ident.py` | identities, the standard kinds and selectors |
| `labrat/term.py` | terminal tables, colour and the interactive shell |
| `labrat/cli.py` | the `labrat` command |
