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
| `args` | object | arguments for `call`, and options for `lock`, `stop` and `adopt` |
| `id` | any | copied into the reply |

Unknown fields are refused, which catches typing mistakes.

## Operations

| Op | Needs | Role | Does |
| --- | --- | --- | --- |
| `health` | | viewer | name, role (`nest` or `rat`), version and uptime, and for a rat its key fingerprint |
| `list` | | viewer | every device, with kind, driver, description and who holds a lock |
| `describe` | `device` | viewer | attributes (type, unit, access, min, max, choices, channels), functions, streams and safe state |
| `get` | `device`, `name` | viewer | read an attribute |
| `set` | `device`, `name`, `value` | operator | write an attribute, after the limit checks |
| `call` | `device`, `name` | operator | run a function with `args` |
| `lock` | `device` | operator | keep a device for yourself for `args.minutes` (default 10), refreshed by use |
| `unlock` | `device` | operator | release it (an admin may release anyone's lock) |
| `stop` | | viewer | every device to its safe state, on the nest and on all its rats at once (locks stay with their holders) |
| `rats` | | viewer | adopted rats with their state, and rats seen on the network but not adopted (nest only) |
| `adopt` | `name`, `value` | admin | adopt a rat with its key, at `args.url` or wherever discovery saw it (nest only) |
| `forget` | `name` | admin | forget an adopted rat (nest only) |

Roles grow from `viewer` to `operator` to `admin`. Each role may do everything the roles before it may.
`stop` is open to every role, so anyone who can see the lab can make it safe.

## Error codes

| Code | HTTP | Meaning |
| --- | --- | --- |
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
