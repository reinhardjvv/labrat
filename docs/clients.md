# Clients

Every client sends the same JSON messages ([api.md](api.md)) to `POST /api/v1/message` with a bearer token.
These examples assume a nest at `http://nest.local:8780` and a token in the environment variable
`LABRAT_TOKEN`.

## Python

```python
from labrat.client import Client

lab = Client("http://nest.local:8780")          # token from LABRAT_TOKEN, or Client(url, token="...")
print(lab.devices())

with lab.locked("bench-rat/psu"):
    lab.set("bench-rat/psu", "VoltageLevel", 3.3, channel="1")
    lab.set("bench-rat/psu", "OutputEnabled", True, channel="1")
    print(lab.call("bench-rat/psu", "Measure", channel="1", type="current"))
```

Find devices by what they are, and keep a handle:

```python
scope = lab.device("oscilloscope tektronix/mso54")
print(scope.describe()["identity"])
psus = lab.find("dc-power location=bench*")
```

Errors arrive as `labrat.message.LabratError`, with `.code` from the error table and a readable `.message`.

Each thread keeps its connection to the nest open between requests. A `bytes` argument, such as a frame for a
display, travels as the raw body of the request instead of base64:

```python
lab.call("optics-rat/slm", "ShowImage", data=frame)          # frame is width x height bytes
```

### Straight to a rat

For fast work with a rat's devices, such as frames for a display or readings from a camera, talk to the rat
directly. The nest still decides who may, under a lease ([safety](safety.md#leases)):

```python
with lab.direct("optics-rat/slm") as slm:      # a lease, checked against the rat, on the fastest address
    print(slm.path)                            # the rat's address that answered first, or "nest"
    for frame in frames:
        slm.call("ShowImage", data=frame)
```

`direct` takes a lease from the nest, asks each of the rat's addresses to prove with the lease's secret that it is
the real rat, uses the one that answered soonest, and renews the lease in the background. If no address
answers, it locks the device and goes through the nest instead. Leaving the block releases the lease and leaves
the device as it is. A script that dies, or a nest that stops answering, lets the lease run out after a minute,
and the rat then puts the device in its safe state.

## MATLAB

MATLAB's `webwrite` sends JSON directly, so no toolbox is needed.

```matlab
url  = "http://nest.local:8780/api/v1/message";
opts = weboptions("MediaType", "application/json", "Timeout", 30, ...
                  "HeaderFields", ["Authorization", "Bearer " + getenv("LABRAT_TOKEN")]);

r = webwrite(url, struct("op", "set", "device", "bench-rat/psu", ...
                         "name", "VoltageLevel", "channel", "1", "value", 3.3), opts);
r = webwrite(url, struct("op", "get", "device", "bench-rat/psu", ...
                         "name", "VoltageLevel", "channel", "1"), opts);
disp(r.value)
```

A refused request makes `webwrite` raise an error. The helper [examples/matlab/labrat.m](../examples/matlab/labrat.m)
reads every reply itself and raises refusals with the identifier `labrat:<code>`, such as `labrat:limit`:

```matlab
lab = labrat("http://nest.local:8780");
lab.set("bench-rat/psu", "VoltageLevel", 3.3, "1");
v = lab.get("bench-rat/psu", "VoltageLevel", "1");

% a pattern computed in MATLAB, shown on an SLM
phase = uint8(mod((0:1919) * 8, 256));
frame = repmat(phase, 1080, 1);
lab.call("optics-rat/slm", "ShowImage", struct("data", matlab.net.base64encode(reshape(frame.', 1, []))));

lab.stop("done for today");
```

## curl and the shell

```bash
curl -s http://nest.local:8780/api/v1/message -H "Authorization: Bearer $LABRAT_TOKEN" \
     -d '{"op": "get", "device": "bench-rat/psu", "name": "VoltageLevel", "channel": "1"}'

curl -sN http://nest.local:8780/api/v1/streams/bench-rat/psu/Readings -H "Authorization: Bearer $LABRAT_TOKEN"
```

The `labrat` command does the same with less typing, in colour on a terminal:

| Command | Does |
| --- | --- |
| `labrat devices [selector] [--by kind]` | devices with kind, manufacturer, model, serial and state, filtered and grouped ([naming.md](naming.md)) |
| `labrat kinds` | the standard device kinds |
| `labrat describe <device>` | every attribute with its current value, unit and allowed range, the functions, streams and safe state |
| `labrat get` / `set` / `call` | read, write and run, with units in the reply |
| `labrat watch <device> [attribute ...]` | live readings, refreshed in place |
| `labrat stream <device> <stream>` | live data to the terminal, or video to a file: `> camera.mjpeg` |
| `labrat shell` | an interactive prompt with history and tab completion of commands, devices and names |
| `labrat approvals`, `approve`, `deny` | requests waiting for a person |
| `labrat record [start / stop / status] <name>` | recordings to storage ([recordings.md](recordings.md)) |

Every command takes `--json` where it prints data, for scripts.

## JavaScript

```js
const reply = await fetch("http://nest.local:8780/api/v1/message", {
  method: "POST",
  headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
  body: JSON.stringify({ op: "list" }),
}).then((r) => r.json());
```

## LabVIEW

Use the HTTP Client VIs. Open a handle, add the header `Authorization` with the value `Bearer <token>`, and
send the message with **POST** to `/api/v1/message`. Build the body with **Flatten To JSON** from a cluster with
the fields `op`, `device`, `name`, `channel` and `value`, and read the reply with **Unflatten From JSON**.

## Other languages

Anything with an HTTP client and a JSON library works: C#, Julia, Rust, Go or an ESP32 running MicroPython. To
make a board serve its own devices as a rat, see [rat-protocol.md](rat-protocol.md).
