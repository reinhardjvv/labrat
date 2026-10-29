# MCP: AI agents in the lab

`labrat mcp` is a Model Context Protocol server. It lets Claude and other MCP clients use a lab through its
nest. Each tool call becomes one LABRAT message, so an agent works under exactly the same token, role, locks
and limits as a person, and the audit log records what it did under its token's name.

## Set up

Make the agent its own token, an operator unless it must adopt rats:

```bash
labrat token --name claude --role operator        # add the printed entry to nest.toml, restart the nest
```

Claude Code:

```bash
claude mcp add labrat -e LABRAT_URL=http://nest.local:8780 -e LABRAT_TOKEN=<token> -- labrat mcp
```

Claude Desktop and other clients, in their MCP configuration:

```json
{
  "mcpServers": {
    "labrat": {
      "command": "labrat",
      "args": ["mcp"],
      "env": { "LABRAT_URL": "http://nest.local:8780", "LABRAT_TOKEN": "<token>" }
    }
  }
}
```

## Tools

| Tool | Message |
| --- | --- |
| `labrat_devices` | `list` |
| `labrat_describe` | `describe` |
| `labrat_get` | `get` |
| `labrat_set` | `set` |
| `labrat_call` | `call` |
| `labrat_lock`, `labrat_unlock` | `lock`, `unlock` |
| `labrat_stop` | `stop`, with the reason marked as coming from an AI agent |
| `labrat_rats` | `rats` |
| `labrat_approval` | `approval`, to follow a request that waits for a person |
| `labrat_who` | `who`: who holds each device and who changed it last |
| `labrat_record` | `record`: list, start, stop and follow recordings to storage |
| `labrat_experiment` | `experiment`: start, note and stop an experiment in the person's folder |
| `labrat_files` | `files`: list the person's experiment folders and read their files back |

Long lists of numbers, such as a scope waveform or a VNA trace, come back to the agent as their size, range,
mean and first values, so they do not fill its context. Scripts using the Python client get the full data.

The server tells the agent the house rules when it connects: describe a device before using it, lock it
before changing it, treat a refused value as a limit that protects equipment and ask a person, and call
`labrat_stop` when anything looks unsafe.

## Guardrails, or none

You set the guardrails you want. If you want to set the rat free and let it run the maze, that is up to you.

| Guardrail | What it does |
| --- | --- |
| The token's role | a viewer token can read and stop the lab but change nothing, an operator token can change devices |
| Limits | ranges in `nest.toml` and `rat.toml` hold whatever the agent asks, and it cannot change them |
| Approvals | chosen requests wait until a person approves them (below) |
| Locks | a device you have locked cannot be changed by the agent |
| The audit log | every change the agent makes is recorded under its token's name |

Approvals are rules in `nest.toml`. This one makes the agent `claude` wait for a person before every change
and every function it asks for:

```toml
[[approvals]]
who = ["claude"]
ops = ["set", "call"]              # every change and every function
```

The agent's request then answers `pending` with an approval id, and the agent tells you what it asked for.
Approve or deny it from any terminal with an operator token:

```bash
labrat approvals
labrat approve a1
```

The request then runs with the agent's own rights, limits and locks. A `stop` denies everything still
waiting. Leave out every `[[approvals]]` rule and give the agent an operator token, and it works on its own.

### God mode

For an agent you trust with everything, set `god_mode = true` on its token:

```toml
[[tokens]]
name = "claude"
role = "operator"
god_mode = true
sha256 = "..."
```

The lab's limits and approval rules then do not apply to it. What stays:

- the instrument's own range, choices and read-only settings, as the instrument would refuse those anyway
- locks: a device someone else holds stays theirs
- `stop`, safe states and the rat watchdog
- the audit log, where every request from the token is marked `"god": true`

God mode is set only in the configuration file. No request can turn it on, and the nest warns about every
god-mode token when it starts. A rat keeps its own limits for the nest's god-mode tokens unless its
`rat.toml` has `allow_god_mode = true`, so whoever owns a bench keeps the last word on it.
