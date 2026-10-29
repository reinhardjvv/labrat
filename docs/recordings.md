# Recordings

A recording writes a device's readings, measurements or stream to storage on a schedule, and formats the
files for you. Recordings are optional: a nest or rat without stores writes nothing.

## Stores

A store is a folder the nest or rat writes into: a local disk or a mounted network share.

```toml
[stores.lab]
path = "/srv/labrat-data"
```

Under systemd, give the `labrat` user write access to the folder and add it to the service with
`ReadWritePaths=/srv/labrat-data`, as the service otherwise sees the system read-only.

### Network shares

Mount the share in `/etc/fstab` (NFS or SMB) and point the store at a folder on it. Two optional settings
keep the data safe when the share drops:

```toml
[stores.nas]
path = "/mnt/labnas/labrat"
mount = "/mnt/labnas"                   # write only while this is mounted
buffer = "/var/lib/labrat/buffer/nas"   # write here first, copy across when the share can be reached
sync_s = 10                             # how often the buffer is copied
```

With `mount` set, a store whose share is missing refuses new files. Data then never lands on the empty mount
point by mistake. With `buffer` set, files are written to the local disk and copied to the share every
`sync_s` seconds. A file leaves the buffer once its copy matches and nothing still writes to it. If the share
drops for an hour, recordings carry on locally and the copies catch up when it returns. `labrat record` shows
each store's state and how many files wait in its buffer.

## What to record

```toml
[recordings.supply-log]          # attributes, every channel they have, once a second, as CSV
store = "lab"
device = "bench-rat/psu"
read = ["VoltageLevel", "OutputEnabled"]
every_s = 1.0
start = true                     # start with the nest

[recordings.scope-traces]        # a function, here a waveform fetch every 10 s, as JSON lines
store = "lab"
device = "scope"
call = "Waveform"
every_s = 10
count = 100                      # stop after 100

[recordings.camera]              # a stream, written as it comes, for one minute
store = "lab"
device = "optics-rat/cam"
stream = "Video"
duration_s = 60
```

| Setting | Default | Meaning |
| --- | --- | --- |
| `store` | | the store to write into |
| `device` | | the device, on the nest or a rat |
| `read`, `call` or `stream` | | attributes to read, a function to call (with `args`), or a stream to write |
| `channel` | every channel | one channel only |
| `every_s` | `1` | time between readings, for `read` and `call` |
| `duration_s`, `count` | none | end after this long, or after this many readings |
| `format` | `csv` for `read`, `jsonl` for `call` | `csv` or `jsonl` (one JSON object per line) |
| `start` | `false` | start when the nest or rat starts |

## Files

Each run writes to `<store>/<recording>/<date>/<recording>-<time>.<ext>`. Next to it,
`<recording>-<time>.meta.json` holds the device's full description with units and ranges, the recording's
settings, who started it, and when it started and ended. Every row carries `time` (ISO 8601, local) and
`unix_s`. Streams keep their own format: MJPEG for video, JSON lines for live readings.

A recording started by someone with an [experiment](experiments.md) open goes into that experiment's folder
instead, and needs no store.

Readings follow `every_s` on a fixed schedule, so a slow device does not make the timing drift. A failed
reading ends the run, and the error goes into the status and the metadata file.

## Starting and stopping

```bash
labrat record                                   # stores, recordings and what is running
labrat record start supply-log
labrat record stop supply-log
labrat record start quick --store lab --device psu --read VoltageLevel --every 0.5 --for 60
```

The last line defines a recording on the spot. The same works from Python (`Client().record("supply-log")`),
from any client with the `record` message, and from an AI agent through `labrat_record`. Starting and stopping
needs an operator token, and a recording reads with the rights of whoever started it. Recordings keep running
through a `stop`, so the data shows what the stop did. They end when the nest or rat shuts down.
