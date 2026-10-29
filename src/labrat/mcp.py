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
2. Lock a device (labrat_lock) before changing it, and unlock it when you are done. A device another person
   holds is theirs: wait or ask them, and see labrat_who for who is using what.
3. A refused value (error 'limit') protects real equipment. Do not reach the same result another way: tell the
   person and ask.
4. Read back what you set. Before switching an output on, say what you are about to do.
5. If anything looks unsafe, call labrat_stop at once. It puts every device in its safe state.
6. The lab may make some of your requests wait for a person (error 'pending' with an approval id). Tell the
   person what you asked for and why, then check labrat_approval until it is approved, denied or expired.
   Never try to get the same effect another way.
7. If the lab keeps experiments, start one (labrat_experiment) before a piece of work and stop it when you are
   done. Your commands, the instrument settings and any recordings then land together in the person's folder.
   Write what you are doing and why as notes. Read results back with labrat_files.
8. To make an unknown device usable: labrat_scan, then labrat_probe (or labrat_capture of its vendor software),
   then write a draft profile, labrat_try_profile it until every attribute reads, and labrat_save_profile it with
   the transcripts. Explain the evidence for each attribute. Never guess at commands that could change or damage
   the device; ask the person. A person adds the device with labrat add."""

_dev = {"type": "string", "description": "Device id from labrat_devices, e.g. 'psu' or 'bench-rat/psu'."}
_ch = {"type": "string", "description": "Channel, for attributes and functions that have channels."}

TOOLS = [
    ("labrat_devices", "The lab's devices with their identity (kind, manufacturer, model, serial, location) and who has "
     "each locked. select filters with a selector such as 'oscilloscope', 'tektronix/*', '#C012345' or 'location=optics*'; "
     "sort orders by device, kind, manufacturer, model, serial, location or rat.",
     {"select": {"type": "string"}, "sort": {"type": "string"}}, [], "list"),
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
    ("labrat_who", "Who holds each device (and for how long), who changed it last and how, and who was active when. "
     "Check it before using a device someone else may be working with.", {}, [], "who"),
    ("labrat_rats", "The rats (computers next to the equipment) the nest has adopted or can see.", {}, [], "rats"),
    ("labrat_record", "Recordings to storage: with no name, the stores and recordings; with a name and action start, stop "
     "or status, that recording. A start may define a new one: device, read (attributes) or call (a function) or stream, "
     "every_s, duration_s, count, store, format (csv or jsonl).",
     {"name": {"type": "string"}, "action": {"type": "string", "enum": ["start", "stop", "status"]},
      "spec": {"type": "object", "description": "settings for a new recording"}}, [], "record"),
    ("labrat_experiment", "Experiments, each in the person's own folder with its commands, instrument settings and "
     "recordings. action: list (the person's experiments), status (the open one), start (name, description), note (text) "
     "or stop.", {"action": {"type": "string", "enum": ["list", "status", "start", "note", "stop"]}, "name": {"type": "string"},
                  "description": {"type": "string"}, "text": {"type": "string"}}, ["action"], "experiment"),
    ("labrat_files", "The person's experiment files. With no path, their experiment folders; with a folder, its files; "
     "with a file, its text (length bytes from offset, 256 KB by default; 'more' says if there is more).",
     {"path": {"type": "string"}, "offset": {"type": "integer"}, "length": {"type": "integer"}}, [], "files"),
    ("labrat_scan", "What is plugged into the nest or a rat (USB devices, serial ports, I2C buses), with the catalog "
     "profiles that fit each. probe = true confirms a fit with the profile's own read-only query.",
     {"rat": {"type": "string"}, "probe": {"type": "boolean"}}, [], "scan"),
    ("labrat_probe", "Ask an unknown device safe questions and get every answer back. target is a serial port, hid:///dev/hidrawN, "
     "tcp://host:port, http://host or i2c:<bus>. With no settings it tries the usual baud rates and protocols. sends adds "
     "queries of your own: those ending in '?' go at once, anything else waits for a person, and words that reset or shut "
     "down a device are never sent. The transcript's file name can be given to labrat_save_profile.",
     {"target": {"type": "string"}, "rat": {"type": "string"}, "protocol": {"type": "string", "enum": ["auto", "scpi", "megatec", "text"]},
      "sends": {"type": "array", "items": {"type": "string"}}, "baud": {"type": "integer"},
      "line_ending": {"type": "string", "enum": ["lf", "cr", "crlf"]}}, ["target"], "probe"),
    ("labrat_capture", "Record what a vendor's software says to a device: start (kind serial with target the port, or usb with "
     "target vendor:product), status, or stop, which returns the transcript's file name. For serial, the person points "
     "the vendor software at the connect_to path.", {"action": {"type": "string", "enum": ["start", "status", "stop"]},
                                                     "kind": {"type": "string", "enum": ["serial", "usb"]}, "target": {"type": "string"},
                                                     "baud": {"type": "integer"}, "rat": {"type": "string"}}, ["action"], "capture"),
    ("labrat_profiles", "The device profile catalog. With id, that profile in full as TOML; otherwise the list, filtered by select.",
     {"id": {"type": "string"}, "select": {"type": "string"}, "rat": {"type": "string"}}, [], "profile"),
    ("labrat_try_profile", "Try a profile on the real device and read every attribute it describes, changing nothing. profile "
     "is a catalog id or a whole draft profile as TOML (labrat_profiles with id mecer/me-2000-wpru shows a complete one); "
     "device holds port or address "
     "and any overrides.", {"profile": {"type": "string"}, "device": {"type": "object"}, "rat": {"type": "string"}},
     ["profile", "device"], "profile"),
    ("labrat_save_profile", "Save a draft profile (TOML, status = \"draft\") to the catalog, with the replies of the named "
     "probe and capture transcripts added as its recordings. A person then adds the device with labrat add.",
     {"toml": {"type": "string"}, "transcripts": {"type": "array", "items": {"type": "string"}}, "rat": {"type": "string"}},
     ["toml"], "profile"),
    ("labrat_approval", "The state of a request that waits for a person: pending, approved (with its result), denied, failed or expired.",
     {"id": {"type": "string", "description": "the approval id from a 'pending' reply"}}, ["id"], "approval"),
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
    if op == "approval":
        msg["name"] = args.get("id")
    if op == "record":
        msg.pop("value", None)
        if args.get("name"):
            msg["args"] = {"action": args.get("action", "status"), **(args.get("spec") or {})}
    if op == "experiment":
        msg.pop("value", None)
        msg["args"] = {k: args[k] for k in ("action", "description", "text") if args.get(k) is not None}
    if op == "files":
        msg = {"op": op, **({"name": args["path"]} if args.get("path") else {}),
               "args": {k: args[k] for k in ("offset", "length") if args.get(k) is not None}}
    if name == "labrat_scan":
        msg = {"op": "scan", "args": {k: args[k] for k in ("rat", "probe") if args.get(k) is not None}}
    if name in ("labrat_probe", "labrat_capture"):
        msg = {"op": op, "args": {k: v for k, v in args.items() if v is not None}}
    if name == "labrat_profiles":
        msg = {"op": "profile", **({"name": args["id"]} if args.get("id") else {}),
               "args": {"action": "show" if args.get("id") else "list", **{k: args[k] for k in ("select", "rat") if args.get(k)}}}
    if name == "labrat_try_profile":
        p = str(args.get("profile", ""))
        msg = {"op": "profile", **({} if "\n" in p else {"name": p}),
               "args": {"action": "try", "device": args.get("device") or {}, **({"toml": p} if "\n" in p else {}),
                        **({"rat": args["rat"]} if args.get("rat") else {})}}
    if name == "labrat_save_profile":
        msg = {"op": "profile", "args": {"action": "save", **{k: args[k] for k in ("toml", "transcripts", "rat") if args.get(k)}}}
    extra = dict(args.get("args") or {})
    if op == "list":
        extra.update({k: args[k] for k in ("select", "sort") if args.get(k)})
    if op == "lock" and "minutes" in args:
        extra["minutes"] = args["minutes"]
    if op == "stop":
        extra["reason"] = f"AI agent: {args.get('reason', '')}"
    if extra:
        msg["args"] = {**msg.get("args", {}), **extra}
    return client.request(op, **{k: v for k, v in msg.items() if k != "op"})


def summarise(value, limit: int = 200):
    """Long lists of numbers (waveforms, traces) as their size, range and first values, to spare the agent's context."""
    if isinstance(value, list) and len(value) > limit and all(isinstance(v, (int, float)) for v in value[:limit]):
        nums = [v for v in value if isinstance(v, (int, float))]
        return {"points": len(value), "min": min(nums), "max": max(nums), "mean": sum(nums) / len(nums), "first": value[:20],
                "note": "summarised; use a script with the Python client for the full data"}
    if isinstance(value, dict):
        return {k: summarise(v, limit) for k, v in value.items()}
    return value


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
            value = summarise(call_tool(client, params.get("name", ""), params.get("arguments") or {}))
            return _reply(id_, {"content": [{"type": "text", "text": json.dumps(value, indent=1, default=str)}]})
        except LabratError as e:
            return _reply(id_, {"content": [{"type": "text", "text": f"{e.code}: {e.message}"}], "isError": e.code != "pending"})
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
