"""Record the README's terminal picture from a real labrat session, in a dark and a light version.

    python tools/terminal_svg.py docs/assets

Starts a nest with simulated devices, runs the commands below with colour forced on, and turns the coloured
output into SVG. The devices carry the identity given in the configuration, as a real lab's would.
"""
from __future__ import annotations

import html
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

COMMANDS = [
    "labrat devices",
    "labrat set heater-psu VoltageLevel 5 -c 1",
    "labrat describe heater-psu",
    "labrat set heater-psu VoltageLevel 20 -c 1",
    "labrat stop",
]

CONFIG = '''name = "optics-nest"
host = "127.0.0.1"
port = {port}
discovery = false
state_dir = "{state}"
[[tokens]]
name = "demo"
role = "admin"
sha256 = "{sha}"
[devices.heater-psu]
driver = "sim-supply"
kind = "dc-power"
manufacturer = "rohde-schwarz"
model = "HMC8042"
serial = "024318"
location = "optics bench"
identify = false
[devices.heater-psu.limits.VoltageLevel]
max = 12
[devices.shutters]
driver = "sim-gpio"
outputs = {{ laser = 17, probe = 27 }}
inputs = {{ door = 22 }}
safe = {{ laser = false }}
location = "optics bench"
identify = false
'''

THEMES = {
    "dark": {"bg": "#0d1117", "bar": "#161b22", "edge": "#30363d", "text": "#e6edf3", "dim": "#7d8590", "accent": "#f2a3c7",
             "good": "#3fb950", "bad": "#ff7b72", "warn": "#d29922", "value": "#79c0ff"},
    "light": {"bg": "#ffffff", "bar": "#f6f8fa", "edge": "#d0d7de", "text": "#1f2328", "dim": "#6e7781", "accent": "#bf3989",
              "good": "#1a7f37", "bad": "#cf222e", "warn": "#9a6700", "value": "#0550ae"},
}
SGR = {"1": ("bold",), "2": ("dim",), "32": ("good",), "31": ("bad",), "33": ("warn",), "1;36": ("value", "bold"),
       "38;5;218": ("accent",)}
MONO = "ui-monospace,SFMono-Regular,Menlo,Consolas,Liberation Mono,monospace"
SANS = "ui-sans-serif,-apple-system,Segoe UI,Helvetica,Arial,sans-serif"
LINE, CHAR, PAD, TOP = 22, 8.13, 20, 58


def record() -> list[list[tuple[str, tuple]]]:
    """[(text, styles)] per line: commands with a prompt, their output, and a blank line between them."""
    from labrat.hub import token_hash
    token, port = secrets.token_urlsafe(12), 8797
    state = Path(tempfile.mkdtemp(prefix="labrat-shot-"))
    (state / "nest.toml").write_text(CONFIG.format(port=port, state=state, sha=token_hash(token)))
    exe = shutil.which("labrat") or str(Path(sys.executable).with_name("labrat"))
    nest = subprocess.Popen([exe, "nest", "--config", str(state / "nest.toml")], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    env = {**os.environ, "LABRAT_URL": f"http://127.0.0.1:{port}", "LABRAT_TOKEN": token, "FORCE_COLOR": "1"}
    env.pop("NO_COLOR", None)
    lines: list[list[tuple[str, tuple]]] = []
    try:
        time.sleep(1.5)
        for i, cmd in enumerate(COMMANDS):
            if i:
                lines.append([])
            lines.append([("$ ", ("accent",)), (cmd, ("bold",))])
            out = subprocess.run([exe, *cmd.split()[1:]], env=env, capture_output=True, text=True)
            for raw in (out.stdout + out.stderr).rstrip("\n").splitlines():
                lines.append(spans(raw))
    finally:
        nest.terminate()
        nest.wait(10)
        shutil.rmtree(state, ignore_errors=True)
    return lines


def spans(raw: str) -> list[tuple[str, tuple]]:
    out, style = [], ()
    for part in re.split(r"(\x1b\[[0-9;]*m)", raw):
        if part.startswith("\x1b["):
            code = part[2:-1]
            style = () if code in ("0", "") else SGR.get(code, style)
        elif part:
            out.append((part, style))
    return out


def svg(lines, theme: str, title: str) -> str:
    t = THEMES[theme]
    cols = max((sum(len(x) for x, _ in ln) for ln in lines), default=40)
    w = max(760, round(PAD * 2 + cols * CHAR))
    h = TOP + (len(lines) - 1) * LINE + 26
    rows = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}" role="img">',
            f"<title>{html.escape(title)}</title>",
            f'<rect x="0.5" y="0.5" width="{w - 1}" height="{h - 1}" rx="10" fill="{t["bg"]}" stroke="{t["edge"]}"/>',
            f'<path d="M0.5 10.5a10 10 0 0 1 10-10h{w - 21}a10 10 0 0 1 10 10v22h-{w - 1}z" fill="{t["bar"]}"/>',
            f'<line x1="0.5" y1="32.5" x2="{w - 0.5}" y2="32.5" stroke="{t["edge"]}"/>',
            '<circle cx="20" cy="16.5" r="6" fill="#ff5f57"/>', '<circle cx="40" cy="16.5" r="6" fill="#febc2e"/>',
            '<circle cx="60" cy="16.5" r="6" fill="#28c840"/>',
            f'<text x="{w / 2}" y="21" text-anchor="middle" font-family="{SANS}" font-size="12" fill="{t["dim"]}">labrat</text>']
    for i, ln in enumerate(lines):
        if not ln:
            continue
        parts = []
        for text, style in ln:
            colour = next((t[s] for s in style if s in ("dim", "good", "bad", "warn", "value", "accent")), t["text"])
            weight = ' font-weight="600"' if "bold" in style else ""
            parts.append(f'<tspan fill="{colour}"{weight}>{html.escape(text)}</tspan>')
        rows.append(f'<text x="{PAD}" y="{TOP + i * LINE}" font-family="{MONO}" font-size="13.5" xml:space="preserve">{"".join(parts)}</text>')
    return "\n".join(rows + ["</svg>", ""])


def main() -> None:
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "docs/assets")
    lines = record()
    title = "labrat in a terminal: the devices, a voltage set, a supply described with its live values and limits, a refused value, and stop"
    for theme in THEMES:
        (out / f"terminal-{theme}.svg").write_text(svg(lines, theme, title), encoding="utf-8")
    print("\n".join("".join(x for x, _ in ln) for ln in lines))


if __name__ == "__main__":
    main()
