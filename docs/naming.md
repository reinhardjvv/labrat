# Names, identities and selectors

Every device has two names.

| | What it is | Example | Who chooses it |
| --- | --- | --- | --- |
| **id** | the short handle you type and script against | `scope`, `psu-left`, `optics-rat/cam` | you, in the configuration |
| **identity** | what the device is | `oscilloscope`, `tektronix`, `MSO54`, `#C012345` | the instrument, or you |

The id can change when you rearrange a lab. The identity stays with the instrument. Its **reference**,
`manufacturer/model#serial`, names one physical instrument wherever it is plugged in, so two identical scopes
stay apart by their serial numbers. A nest that sees the same reference twice marks the second as
`duplicate_of` the first, which catches an instrument configured on two rats.

## Ids

Use lowercase letters, digits and `-`. Name a device for its job or place when there are several of a kind
(`scope-left`, `psu-heater`), and for its kind when there is one (`scope`, `vna`). Devices on a rat are
`<rat>/<device>` at the nest, so the rat's name already says where it is.

## Identity

| Field | Meaning | From |
| --- | --- | --- |
| `kind` | the standard kind, below | the configuration, or the driver |
| `manufacturer` | the maker, normalised: `TEKTRONIX` and `Tektronix, Inc.` both become `tektronix` | `*IDN?` or the configuration |
| `model` | the model as the maker writes it | `*IDN?` or the configuration |
| `serial` | the serial number | `*IDN?` or the configuration |
| `firmware` | the firmware version | `*IDN?` or the configuration |
| `location` | where it is | the configuration |
| `tags` | your own labels, such as `teaching` or `shared` | the configuration |

The `scpi` driver asks `*IDN?` the first time the identity is needed, and keeps the answer. Set
`identify = false` on any device to use only the identity in its configuration. Fields in the configuration always win.

```toml
[devices.scope-left]
driver = "scpi"
address = "tcp://192.0.2.11:4000"
kind = "oscilloscope"
location = "optics table"
tags = ["shared"]
```

## Kinds

`labrat kinds` lists them. They follow the IVI Foundation's instrument classes where one exists. Common other
names are accepted and stored as the standard one: `scope` is stored as `oscilloscope`, `spectrum-analyzer` as
`spectrum-analyser`, `psu` as `dc-power`, `awg` as `function-generator` and `slm` as `display`. An unknown kind
is an error that names the nearest standard kinds.

| Kind | For |
| --- | --- |
| `oscilloscope` | oscilloscopes |
| `function-generator` | function and arbitrary waveform generators |
| `rf-signal-generator` | RF and microwave signal generators |
| `spectrum-analyser` | spectrum and signal analysers |
| `vna` | vector network analysers |
| `dc-power`, `ac-power` | power supplies and sources |
| `electronic-load`, `source-meter` | loads and source measure units |
| `dmm`, `power-meter`, `counter` | meters and counters |
| `digitiser`, `data-logger` | acquisition cards and loggers |
| `switch` | switch matrices and multiplexers |
| `camera`, `display` | cameras, and SLMs, DMDs and projectors |
| `laser`, `motion`, `temperature-controller` | lasers, stages and motors, temperature controllers |
| `gpio`, `sensor`, `ups` | GPIO lines, other sensors, UPSes |
| `instrument` | anything whose kind is not set |

## Selectors

A selector finds devices by their identity, in the style of Spack's package specs.

| Selector | Finds |
| --- | --- |
| `oscilloscope` | every oscilloscope |
| `tektronix/mso54` | every Tektronix MSO54 |
| `oscilloscope tektronix/*` | every Tektronix oscilloscope |
| `#C012345` | the instrument with that serial number |
| `@1.40` | firmware 1.40 |
| `location=optics* tag=shared` | any identity field, with `*` and `?` patterns |
| `scope-*` | a word that is not a kind matches device ids |

Words combine, and every one must match. Selectors work everywhere:

```bash
labrat devices oscilloscope tektronix/*
labrat devices --by kind                 # grouped: kind, manufacturer, model, location or rat
labrat devices --sort manufacturer
```

```python
scope = lab.device("oscilloscope location=optics*")     # exactly one match, or an error naming them all
scope.set("HorizontalScale", 1e-6)
ids = lab.find("dc-power")                               # every match
```

In messages, `list` takes `args.select` and `args.sort`, and AI agents use the same in `labrat_devices`.
