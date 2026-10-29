"""MCP server: lets an AI agent (Claude, or any MCP client) use a lab through its nest.

    claude mcp add labrat -e LABRAT_URL=http://nest.local:8780 -e LABRAT_TOKEN=<token> -- labrat mcp

It speaks the Model Context Protocol (JSON-RPC 2.0 over stdio) and turns every tool call into one LABRAT
message. The agent therefore works under the same token, role, locks and limits as any other client, and the
nest's audit log records what it did under the token's name. Give an agent its own token.
"""
from __future__ import annotations

import json
import sys

from . import __version__
from .client import Client
from .message import LabratError

PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")

INSTRUCTIONS = """LABRAT connects you to real laboratory equipment. Rules:
1. Call labrat_devices, then labrat_describe each device you will use. Use only the attribute and function names,
   channels and units it lists.
2. Lock a device (labrat_lock) before changing it, and unlock it when you are done.
3. A refused value (error 'limit') protects real equipment. Do not reach the same result another way: tell the
   person and ask.
4. Read back what you set. Before switching an output on, say what you are about to do.
5. If anything looks unsafe, call labrat_stop at once. It puts every device in its safe state."""

_dev = {"type": "string", "description": "Device id from labrat_devices, e.g. 'psu' or 'bench-rat/psu'."}
_ch = {"type": "string", "description": "Channel, for attributes and functions that have channels."}

TOOLS = [
    ("labrat_devices", "Every device in the lab, with its kind and who has it locked.", {}, [], "list"),
    ("labrat_describe", "What a device offers: attributes (type, unit, limits, channels), functions and its safe state.",
     {"device": _dev}, ["device"], "describe"),
    ("labrat_get", "Read an attribute.", {"device": _dev, "name": {"type": "string"}, "channel": _ch}, ["device", "name"], "get"),
    ("labrat_set", "Set an attribute. Values outside the device's limits are refused.",
     {"device": _dev, "name": {"type": "string"}, "value": {"description": "number, boolean or string"}, "channel": _ch},
     ["device", "name", "value"], "set"),
    ("labrat_call", "Run a device function, with arguments as described by labrat_describe.",
     {"device": _dev, "name": {"type": "string"}, "channel": _ch, "args": {"type": "object"}}, ["device", "name"], "call"),
    ("labrat_lock", "Take a device for yourself for some minutes (10 by default) so nobody else changes it.",
     {"device": _dev, "minutes": {"type": "number"}}, ["device"], "lock"),
    ("labrat_unlock", "Release a device you locked.", {"device": _dev}, ["device"], "unlock"),
    ("labrat_stop", "Put every device in the lab in its safe state. Use it whenever something looks unsafe.",
     {"reason": {"type": "string"}}, ["reason"], "stop"),
    ("labrat_rats", "The rats (small computers next to the equipment) the nest has adopted or can see.", {}, [], "rats"),
]


def tool_list() -> list[dict]:
    return [{"name": n, "description": d, "inputSchema": {"type": "object", "properties": p, "required": r}}
            for n, d, p, r, _ in TOOLS]


def call_tool(client: Client, name: str, args: dict):
    spec = next((t for t in TOOLS if t[0] == name), None)
    if spec is None:
        raise LabratError("not_found", f"no tool {name}")
    op = spec[4]
    msg: dict = {"op": op}
    for k in ("device", "name", "channel", "value"):
        if k in args:
            msg[k] = args[k]
    extra = dict(args.get("args") or {})
    if op == "lock" and "minutes" in args:
        extra["minutes"] = args["minutes"]
    if op == "stop":
        extra["reason"] = f"AI agent: {args.get('reason', '')}"
    if extra:
        msg["args"] = extra
    return client.request(op, **{k: v for k, v in msg.items() if k != "op"})


def _reply(id_, result=None, error=None) -> dict:
    out = {"jsonrpc": "2.0", "id": id_}
    out["error" if error else "result"] = error or result
    return out


def handle(client: Client, req: dict) -> dict | None:
    """One JSON-RPC message in, its response out (None for notifications)."""
    id_, method, params = req.get("id"), req.get("method"), req.get("params") or {}
    if id_ is None:
        return None
    if method == "initialize":
        asked = params.get("protocolVersion")
        return _reply(id_, {"protocolVersion": asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
                            "capabilities": {"tools": {}}, "serverInfo": {"name": "labrat", "version": __version__},
                            "instructions": INSTRUCTIONS})
    if method == "ping":
        return _reply(id_, {})
    if method == "tools/list":
        return _reply(id_, {"tools": tool_list()})
    if method == "tools/call":
        try:
            value = call_tool(client, params.get("name", ""), params.get("arguments") or {})
            return _reply(id_, {"content": [{"type": "text", "text": json.dumps(value, indent=1, default=str)}]})
        except LabratError as e:
            return _reply(id_, {"content": [{"type": "text", "text": f"{e.code}: {e.message}"}], "isError": True})
    return _reply(id_, error={"code": -32601, "message": f"method not found: {method}"})


def run(client: Client, stdin=None, stdout=None) -> None:
    stdin, stdout = stdin or sys.stdin, stdout or sys.stdout
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except ValueError:
            out = _reply(None, error={"code": -32700, "message": "parse error"})
        else:
            out = handle(client, req) if isinstance(req, dict) else _reply(None, error={"code": -32600, "message": "invalid request"})
        if out is not None:
            stdout.write(json.dumps(out) + "\n")
            stdout.flush()
