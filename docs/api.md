# Message API

Every part of LABRAT speaks one message. A client sends a JSON object naming an operation, and gets back a
JSON object that says whether it worked. The nest, every rat, the Python client, the command line and the MCP
server all use the same messages.

## Sending a message

```
POST /api/v1/message
Authorization: Bearer <token>
Content-Type: application/json

{"op": "get", "device": "bench-rat/psu", "name": "VoltageLevel", "channel": "1"}
```

```json
{"ok": true, "value": 3.3, "unit": "V"}
```

A failed request has `ok` false and an error with a stable code:

```json
{"ok": false, "error": {"code": "limit", "message": "VoltageLevel 20 V is above the limit of 12 V"}}
```

The body may also be a list of up to 100 messages. They run in order and the reply is a list of replies in
the same order, with HTTP status 200. Add an `id` to any message to have it copied into its reply.

## Fields

| Field | Type | Meaning |
| --- | --- | --- |
| `op` | string | the operation, from the table below |
| `device` | string | a device id from `list`, such as `psu` or `bench-rat/psu` |
| `name` | string | an attribute or function name from `describe`, or a rat's name for `adopt` and `forget` |
| `channel` | string | the channel, for attributes and functions that have channels |
| `value` | any | the value for `set`, or the rat's key for `adopt` |
| `args` | object | arguments for `call`, and options for `lock`, `stop`, `adopt`, `record`, `experiment` and `files` |
| `id` | any | copied into the reply |

Unknown fields are refused, which catches typing mistakes.

## Operations

| Op | Needs | Role | Does |
| --- | --- | --- | --- |
| `health` | | viewer | name, role (`nest` or `rat`), version and uptime, and for a rat its key fingerprint |
| `list` | | viewer | every device with its identity (kind, manufacturer, model, serial, firmware, location, tags, ref), driver, description and lock; `args.select` filters with a selector and `args.sort` orders ([naming.md](naming.md)) |
| `describe` | `device` | viewer | identity, attributes (type, unit, access, min, max, choices, channels), functions, streams and safe state |
| `get` | `device`, `name` | viewer | read an attribute |
| `set` | `device`, `name`, `value` | operator | write an attribute, after the limit checks |
| `call` | `device`, `name` | operator | run a function with `args` |
| `lock` | `device` | operator | keep a device for yourself for `args.minutes` (default 10), refreshed by use |
| `unlock` | `device` | operator | release it (an admin may release anyone's lock) |
| `stop` | | viewer | every device to its safe state, on the nest and on all its rats at once (locks stay with their holders) |
| `rats` | | viewer | adopted rats with their state, and rats seen on the network but not adopted (nest only) |
| `adopt` | `name`, `value` | admin | adopt a rat with its key, at `args.url` or wherever discovery saw it (nest only) |
| `forget` | `name` | admin | forget an adopted rat (nest only) |
| `record` | `name`, `args.action` | viewer, operator to start or stop | without a name: the stores and recordings. With a name: `start` (optionally with a new recording's settings in `args`), `stop` or `status` ([recordings.md](recordings.md)) |
| `experiment` | `args.action` | viewer, operator to start, stop or note | `list` (yours, or `args.person` for an admin, `*` for all), `status`, `start` with `name` and `args.description`, `note` with `args.text`, or `stop` ([experiments.md](experiments.md)) |
| `who` | | viewer | the devices someone holds or has changed: holder, minutes left, and the last change (who, when, what), and when each person was last active |
| `users` | `args.action` | viewer to list, admin to add or remove | `list`, `add` with `name`, `args.role` and `args.for` (the reply holds the new token, once), or `remove` with `name` (nest only) |
| `scan` | | viewer, operator with `args.probe` | what is plugged in and the profiles that fit; `args.rat` runs it on a rat ([catalog](catalog.md)) |
| `probe` | `args.target` | operator | safe questions to a device: `args.protocol`, `args.baud`, `args.line_ending`, `args.sends` (non-queries wait for approval) |
| `capture` | `args.action` | operator | `start` with `args.kind` (`serial` or `usb`) and `args.target`, `status`, or `stop`, which saves the transcript |
| `profile` | `args.action` | viewer to list or show, operator to try or save | `list` (`args.select`), `show` with `name`, `try` with `name` or `args.toml` and `args.device`, `save` with `args.toml` and `args.transcripts` |
| `device` | `name`, `args.action` | admin | `add` with `args.config` (a profile or driver and its settings), or `remove`; `rat/name` acts on a rat |
| `files` | | viewer | with `name` a path in your folder: a folder's files, or part of a file (`args.offset`, `args.length`, up to 4 MB) as `text` or `base64`, with `more`. Admins may give `args.person` |
| `approvals` | | viewer | requests waiting for a person |
| `approval` | `name` (the id) | viewer | one request's state: pending, approved with its result, denied, failed or expired |
| `approve` | `name` (the id) | operator | approve a request someone else made; it then runs with their rights |
| `deny` | `name` (the id) | operator | deny it |

Roles grow from `viewer` to `operator` to `admin`. Each role may do everything the roles before it may.
`stop` is open to every role, so anyone who can see the lab can make it safe.

## Error codes

| Code | HTTP | Meaning |
| --- | --- | --- |
| `pending` | 202 | the request waits for a person to approve it; `approval` in the error holds its id |
| `bad_request` | 400 | the message is malformed, or a value has the wrong type |
| `unauthorised` | 401 | no token, or an unknown one |
| `forbidden` | 403 | the token's role may not do this, or the attribute is read-only |
| `not_found` | 404 | no such device, attribute, function, channel or rat |
| `limit` | 422 | the value is outside the limits, and nothing was sent to the device |
| `locked` | 423 | someone else holds the device's lock |
| `internal` | 500 | a bug in LABRAT or a driver, with the details in the server log |
| `device` | 502 | the device did not answer, or answered with an error |
| `unavailable` | 503 | a rat or a needed program is out of reach |

## Values and units

Numbers are SI units and the unit is in every `describe` and `get` reply: V, A, Hz, s, dB, px. Booleans are
JSON `true` and `false`. The strings `"true"`, `"false"`, `"on"`, `"off"`, `"yes"`, `"no"`, `"1"` and `"0"` and
the numbers 0 and 1 are accepted for booleans too. Not a number and infinity are refused.

## Read-only routes

| Route | Same as |
| --- | --- |
| `GET /api/v1/health[?challenge=<text>]` | `health`, without a token, for discovery and monitoring. A rat adds `proof`, the HMAC-SHA256 of the challenge under its key |
| `GET /api/v1/devices` | `list` |
| `GET /api/v1/devices/<device>` | `describe` |

`GET /api/v1/files/<path>[?person=<person>]` sends a whole file from your experiments, for downloads of any
size (`labrat files <path> --get`, `Client.download`).

## Streams

Devices with continuous data list their streams in `describe`:

```json
"streams": {"Video": {"content_type": "multipart/x-mixed-replace; boundary=labratframe", "doc": "live MJPEG"}}
```

Open one with a plain GET and read until you have enough:

```
GET /api/v1/streams/<device>/<stream>[?channel=<channel>]
Authorization: Bearer <token>
```

The reply carries the stream's content type and comes in chunks as the device produces data. A camera sends
MJPEG, which browsers and most video tools show directly. Live readings come as newline-delimited JSON, one
object per line. Watching a stream needs the viewer role. The nest passes rat streams through, and serves at
most 16 streams at a time.

## Versioning

The API is versioned in its path. Additions such as new operations, fields and error codes may arrive within
`/api/v1`. Anything that would break a working client gets a new version, and the old one stays for at
least one minor release.
