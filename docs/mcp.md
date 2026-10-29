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

The server tells the agent the house rules when it connects: describe a device before using it, lock it
before changing it, treat a refused value as a limit that protects equipment and ask a person, and call
`labrat_stop` when anything looks unsafe.

## Keeping a person in charge

The token's role decides what an agent can do. A viewer token lets it read and stop the lab, but change
nothing. Limits set in `nest.toml` hold whatever the agent asks, and it cannot change them. An
approval step for chosen actions is on the [roadmap](roadmap.md).
