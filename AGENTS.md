# Instructions for AI agents

This file is for AI coding agents working on LABRAT, and for agents operating a lab through it.

## Commands

```bash
.venv/bin/pip install -e '.[dev]'
.venv/bin/pytest                         # every test, without hardware
.venv/bin/ruff check src tests tools
.venv/bin/python tools/check_repo.py     # private data and wording
.venv/bin/labrat demo                    # a nest with simulated devices
```

Read [docs/architecture.md](docs/architecture.md) for the code map before changing code.

## Rules for changing the code

1. **Never weaken safety.** Do not loosen limits, skip `Attr.check`, make a safe state optional, widen a
   role's rights or let raw commands bypass the checks. If a task seems to need this, stop and ask.
2. **Never invent facts about equipment.** Ranges, ratings and commands come from the manufacturer's
   documents or from a recorded test. Leave out what you cannot confirm.
3. **Nothing private.** No real IP addresses (use 192.0.2.0/24), host names, user names, home paths, serial
   numbers, tokens or keys, and nothing from the lab you happen to be running in.
4. **Tests run without hardware.** Add a test for every change, with simulated or fake devices.
5. **Plain writing.** Short, factual sentences in UK spelling. No promotional language and no filler.
6. **Match the code around you.** Same naming, same comment density, standard library first.

## Rules for operating a lab

Use the MCP server ([docs/mcp.md](docs/mcp.md)) or `labrat.client.Client`. Describe a device before using it,
lock it before changing it, read back what you set, and treat a `limit` error as a limit that protects real
equipment: report it, do not work around it. Call `stop` when anything looks unsafe.
