"""Terminal output for the labrat command: colour, tables and the interactive shell.

Colour is used on a terminal unless NO_COLOR is set, and can be forced with FORCE_COLOR (for recordings).
"""
from __future__ import annotations

import os
import re
import shlex
import sys

ANSI = re.compile(r"\x1b\[[0-9;]*m")


class Style:
    def __init__(self, on: bool):
        self.on = on

    def _c(self, code: str, text) -> str:
        return f"\x1b[{code}m{text}\x1b[0m" if self.on else str(text)

    def bold(self, t):
        return self._c("1", t)

    def dim(self, t):
        return self._c("2", t)

    def accent(self, t):
        return self._c("38;5;218", t)            # the rat's pink

    def good(self, t):
        return self._c("32", t)

    def bad(self, t):
        return self._c("31", t)

    def warn(self, t):
        return self._c("33", t)

    def value(self, t):
        return self._c("1;36", t)


def colour_wanted(stream=None) -> bool:
    stream = stream or sys.stdout
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    return stream.isatty() and os.environ.get("TERM") != "dumb"


def width(text: str) -> int:
    return len(ANSI.sub("", text))


def table(headers: list[str], rows: list[list[str]], st: Style, indent: int = 2) -> str:
    """Columns sized to their contents, headers dimmed, no borders."""
    cols = len(headers)
    w = [max([width(headers[i])] + [width(r[i]) for r in rows]) for i in range(cols)]
    pad = " " * indent

    def line(cells):
        return pad + "   ".join(c + " " * (w[i] - width(c)) for i, c in enumerate(cells)).rstrip()
    return "\n".join([line([st.dim(h) for h in headers])] + [line(r) for r in rows])


def quantity(value, unit: str, st: Style) -> str:
    if isinstance(value, bool):
        return st.good("on") if value else st.dim("off")
    if isinstance(value, float):
        value = f"{value:g}"
    return st.value(value) + (f" {st.dim(unit)}" if unit else "")


def span(spec: dict) -> str:
    lo, hi, unit = spec.get("min"), spec.get("max"), spec.get("unit", "")
    if spec.get("choices"):
        return " | ".join(map(str, spec["choices"]))
    if lo is None and hi is None:
        return ""
    lo = "" if lo is None else f"{lo:g}"
    hi = "" if hi is None else f"{hi:g}"
    return f"{lo} .. {hi} {unit}".strip()


# -- the interactive shell ----------------------------------------------------------------------------------
SHELL_HELP = """commands: devices, describe <device>, get <device> <attribute> [-c ch], set <device> <attribute> <value> [-c ch],
          call <device> <function> [k=v ...] [-c ch], watch <device> [attribute ...], lock, unlock, rats,
          approvals, approve <id>, deny <id>, record [start|stop|status] <name>, experiment [start|stop|note|status],
          experiments, files [path], who, users, stop, help, exit. Tab completes commands, devices and names."""
COMMANDS = ["devices", "describe", "get", "set", "call", "watch", "lock", "unlock", "rats", "approvals", "approve", "deny", "record",
            "experiment", "experiments", "files", "who", "users", "stop", "help", "exit"]


def shell(client, run) -> int:
    """A prompt that runs labrat commands against one nest, with completion of devices and names."""
    try:
        import readline
    except ImportError:                          # not on every platform; the shell still works without it
        readline = None
    st = Style(colour_wanted())
    names: dict[str, list[str]] = {}

    def devices() -> list[str]:
        try:
            return [d["device"] for d in client.devices()]
        except Exception:
            return []
    known = devices()

    def members(device: str) -> list[str]:
        if device not in names:
            try:
                d = client.describe(device)
                names[device] = list(d.get("attributes", {})) + list(d.get("functions", {})) + list(d.get("streams", {}))
            except Exception:
                names[device] = []
        return names[device]

    def complete(text, state):
        line = readline.get_line_buffer()
        parts = line.split()
        if line.endswith(" "):
            parts.append("")
        if len(parts) <= 1:
            options = COMMANDS
        elif len(parts) == 2:
            options = known
        else:
            options = members(parts[1])
        hits = [o for o in options if o.startswith(text)]
        return hits[state] if state < len(hits) else None

    if readline is not None:
        readline.set_completer(complete)
        readline.set_completer_delims(" ")
        readline.parse_and_bind("tab: complete")
        history = os.path.expanduser("~/.labrat_history")
        try:
            readline.read_history_file(history)
        except OSError:
            pass
    health = client.health()
    print(f"{st.accent('labrat')} {st.dim('connected to')} {health['role']} {st.bold(health['name'])} "
          f"{st.dim(client.url)}  {st.dim('(help, tab to complete, Ctrl-D to leave)')}")
    prompt = (f"\x01\x1b[38;5;218m\x02{health['name']}\x01\x1b[0m\x02 > " if st.on else f"{health['name']} > ")
    try:
        while True:
            try:
                line = input(prompt).strip()
            except EOFError:
                print()
                break
            except KeyboardInterrupt:
                print()
                continue
            if not line:
                continue
            if line in ("exit", "quit"):
                break
            if line == "help":
                print(SHELL_HELP)
                continue
            try:
                run(shlex.split(line))
            except SystemExit:
                pass
            except KeyboardInterrupt:
                print()
    finally:
        if readline is not None:
            try:
                readline.write_history_file(history)
            except OSError:
                pass
    return 0
