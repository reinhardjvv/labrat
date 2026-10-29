# Rat protocol

Anything that does the following is a rat, and a nest can adopt it: a Raspberry Pi running `labrat rat`, an
ESP32 with its own firmware, or a commercial instrument with the protocol built in.

## Announce

Register a multicast DNS service of type `_labrat._tcp` on the rat's HTTP port, with these TXT records:

| Key | Value |
| --- | --- |
| `role` | `rat` |
| `name` | the rat's name: letters, digits, `-` and `_`, at most 63 |
| `version` | the firmware or software version |
| `fp` | the key fingerprint: the first 12 hex digits of SHA-256 of the key, in groups of four joined by `-` |

## Authenticate

Make a random key of at least 128 bits on first start and keep it. Show it to the person setting the rat up,
on a serial console, a display or a web page. Accept `Authorization: Bearer <key>` on every route except
health.

## Answer

| Route | Must |
| --- | --- |
| `GET /api/v1/health` | answer `{"ok": true, "value": {"name", "role": "rat", "version", "fingerprint"}}` without a token, and with `?challenge=<text>` add `"proof"`: the hex HMAC-SHA256 of the challenge with the key as the HMAC key |
| `POST /api/v1/message` | answer the operations `health`, `list`, `describe`, `get`, `set`, `call` and `stop` as in [api.md](api.md) |

`describe` must list every attribute with its `type`, `access` and, for numbers, `min` and `max`. The nest
uses these to check changes before they reach the rat. The rat must check them again.

`stop` must put every output in its safe state and answer `{"ok": true, "value": {"safe": true, "problems": []}}`,
or list the problems met.

## Watchdog

A rat should go to its safe state when it has changed an output and has had no authenticated request for a set
time. The nest sends `health` to every adopted rat every few seconds, so 30 s leaves a wide margin.

## Check a rat

`labrat find` shows whether the announcement works. `labrat adopt <name> --at http://<rat>:<port>` and
then `labrat devices`, `labrat describe <name>/<device>` and `labrat stop` exercise the rest.
