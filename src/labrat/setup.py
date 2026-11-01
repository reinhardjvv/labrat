"""labrat setup: make this computer part of a lab in one command.

    labrat setup                     # asks what this computer should be, then asks the nest to let it in
    labrat setup --key lk_...        # joins at once with a join key an admin made
    labrat setup --yes --role worker --url http://nest.local:8780     # no questions, every default
    labrat setup --dry-run           # shows what it would do and changes nothing
    labrat setup --undo              # takes back what it did

It looks at the computer (CPUs, memory, GPUs, network shares, displays and instruments plugged in), finds the
nest on the network, asks a few questions with sensible defaults, shows a summary, and only then writes the
worker's and rat's configuration, makes their keys, asks the nest to let the computer in and installs services
that keep them running: systemd on Linux (system services as root, user services otherwise), launchd on a Mac.

The computer never needs anyone's token. It sends the nest the keys it made for itself and waits for a person to
approve the code it shows (labrat approve <code>), or uses a join key (joins.py). Everything it writes is listed
in <state_dir>/setup.json, which --undo reads.
"""
from __future__ import annotations

import json
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import textwrap
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from . import __version__
from . import config as conf
from .message import LabratError

# -- the rat, in the colours of the logo -----------------------------------------------------------------------------
RAT_COLOURS = {"a": (138, 138, 150), "b": (220, 220, 228), "c": (228, 138, 179), "d": (242, 163, 199), "e": (246, 196, 220),
               "f": (246, 246, 248), "g": (255, 255, 255)}
RAT = ["..................dddd...............", ".............fffffddcdd..............", "..........ffffffffdcccd..............",
       "........ffffffffffdcccd..............", ".......ffffffffffffdddff.............", "......fffffffffffffffffff............",
       "......ffffffffffffffffffff...........", ".....ffffffffffffffffffffff..........", ".....ffffffffffffffffff.gfff......aa.",
       "....fffffffffffffffffff..fffff.aaa...", "....fffffffffffffffffff..fffffdaaaaaa", "....fffffffffffffffffeefffffffd......",
       "....ffffffffffffffffffffffffffaaa....", ".....ffffffffffffffffffffff......aa..", "....dfffffffbbbbbbfffffff............",
       "...dddfbbbbbbbbbbbbbbbbd.............", ".dddd...bbdddbbbbbbbb.dd.............", ".dd......ddddd.......................",
       "dd...................................", "dd...................................", "d....................................",
       "dd.......ddddd.......................", ".ddd..dddd...........................", "..ddddd.............................."]
CODE = re.compile(r"^[A-Za-z]{4}-\d{4}$")


class Ui:
    """The terminal: colours, the rat, sections, questions and the installer's steps. Without a terminal (or with
    --yes) it prints plain lines and takes every default."""

    def __init__(self, interactive: bool, fast: bool = False):
        self.tty = sys.stdout.isatty()
        self.interactive = interactive and sys.stdin.isatty() and self.tty
        self.fast = fast or not self.tty
        cols = shutil.get_terminal_size((80, 24)).columns
        self.cols, self.width, self.label = cols, max(40, min(64, cols - 4)), 26
        e = "\x1b[" if self.tty else None
        self.E = e or ""
        c = (lambda code: f"{e}{code}m") if e else (lambda code: "")
        self.pink, self.green, self.dim, self.bold = c("38;2;242;163;199"), c("38;2;111;207;151"), c("38;2;135;131;148"), c("1")
        self.yellow, self.red, self.reset, self.sel = c("38;2;242;201;76"), c("38;2;235;87;87"), c("0"), c("38;2;22;21;27;48;2;242;163;199")

    # -- output -----------------------------------------------------------------------------------------
    def w(self, s: str = "") -> None:
        sys.stdout.write(s)
        sys.stdout.flush()

    def pause(self, s: float) -> None:
        if not self.fast:
            time.sleep(s)

    def line(self, s: str = "", pause: float = 0.06) -> None:
        self.w("  " + s + "\n" if s else "\n")
        self.pause(pause)

    def section(self, title: str) -> None:
        self.line()
        self.line(f"{self.bold}{title}{self.reset}", 0.1)

    def row(self, key: str, value: str) -> None:
        if len(self.plain(value)) > self.width - self.label:
            value = self.fit(self.plain(value))
        self.line(f"  {self.dim}{key:<{self.label - 2}}{self.reset}{value}", 0.08)

    def note(self, text: str) -> None:
        for part in textwrap.wrap(text, self.width - 6):
            self.line(f"    {self.dim}{part}{self.reset}", 0.03)

    def ok(self, text: str) -> None:
        self.line(f"  {self.green}✓{self.reset} {text}", 0.1)

    def fail(self, text: str) -> None:
        self.line(f"  {self.red}✗{self.reset} {text}", 0)

    def fit(self, text: str, room: int | None = None) -> str:
        """A value made to fit on its line: the home folder as ~, and the middle of a long path left out."""
        room = room or self.width - self.label
        text = str(text).replace(str(Path.home()), "~", 1) if str(Path.home()) != "/" else str(text)
        if len(text) <= room:
            return text
        if "/" in text:
            head, tail = text[:room // 3], text[-(room - room // 3 - 1):]
            return head + "…" + tail
        return text[:room - 1] + "…"

    def plain(self, s: str) -> str:
        return re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", s)

    def rule(self) -> None:
        self.line(f"{self.dim}{'─' * self.width}{self.reset}", 0)

    def header(self, subtitle: str) -> None:
        side = {5: f"{self.bold}LABRAT setup{self.reset}", 6: f"{self.dim}{subtitle}{self.reset}",
                8: f"{self.dim}Version {__version__}{self.reset}"}
        if not self.tty:
            self.line(f"LABRAT setup {__version__}: {subtitle}")
            self.rule()
            return
        lines, E = self.rat_lines(), self.E
        beside = self.cols >= 2 + 37 + 5 + 30
        self.w("\n" + "\n" * len(lines))
        for off in ([2] if self.fast else list(range(min(self.cols, 110) - 40, 2, -4)) + [2]):
            self.w(f"{E}{len(lines)}A")
            for i, s in enumerate(lines):
                self.w(f"{E}2K" + " " * off + s + ("     " + side[i] if beside and off == 2 and i in side else "") + "\n")
            time.sleep(0 if self.fast else 0.018)
        if not beside:
            self.line()
            for i in sorted(side):
                self.line(side[i], 0)
        self.line()
        self.rule()

    def rat_lines(self) -> list[str]:
        out, E, R = [], self.E, self.reset
        for r in range(0, len(RAT), 2):
            s = ""
            for col in range(len(RAT[0])):
                a, b = RAT_COLOURS.get(RAT[r][col]), RAT_COLOURS.get(RAT[r + 1][col]) if r + 1 < len(RAT) else None
                if a and b:
                    s += f"{E}38;2;{a[0]};{a[1]};{a[2]}m{E}48;2;{b[0]};{b[1]};{b[2]}m▀{R}"
                elif a:
                    s += f"{E}38;2;{a[0]};{a[1]};{a[2]}m▀{R}"
                elif b:
                    s += f"{E}38;2;{b[0]};{b[1]};{b[2]}m▄{R}"
                else:
                    s += " "
            out.append(s)
        return out

    def panel(self, title: str, rows: list[str]) -> None:
        inner = self.width - 4
        self.line(f"{self.dim}┌─ {self.reset}{self.bold}{title}{self.reset} {self.dim}{'─' * max(1, inner - len(title) - 1)}┐{self.reset}", 0.02)
        for r in rows:
            self.line(f"{self.dim}│{self.reset}  {r}{' ' * max(0, inner - len(self.plain(r)))}{self.dim}│{self.reset}", 0.02)
        self.line(f"{self.dim}└{'─' * (inner + 2)}┘{self.reset}")

    # -- input ------------------------------------------------------------------------------------------
    def key(self) -> str:
        import termios
        import tty
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        try:
            tty.setraw(fd)
            ch = sys.stdin.read(1)
            if ch == "\x1b":
                ch += sys.stdin.read(2)
            if ch == "\x03":
                raise KeyboardInterrupt
            return ch
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)

    def prompt(self, question: str) -> str:
        return f"\r{self.E}2K    {self.pink}?{self.reset}  {question:<{self.label - 3}}"

    def answered(self, question: str, answer: str) -> None:
        if self.tty:
            self.w(self.prompt(question) + f" {answer}{self.E}K\n")
        else:
            self.w(f"    ?  {question:<{self.label - 3}} {answer}\n")

    def choose(self, question: str, options: list[str], default: int = 0, multi: bool = False, ticked: list | None = None):
        """One of several (or several, with multi), one option per line so nothing wraps. Up and down, space to
        tick, Enter to choose. Without a terminal, the default."""
        on = list(ticked) if ticked is not None else [True] * len(options)
        if not self.interactive:
            picked = [o for o, x in zip(options, on, strict=True) if x] if multi else options[default]
            self.answered(question, (", ".join(picked) or "none") if multi else picked)
            return picked
        E, i, n = self.E, default, len(options)
        self.w(self.prompt(question) + f" {self.dim}{'up, down, space, enter' if multi else 'up, down, enter'}{self.reset}\n" + "\n" * n)
        while True:
            self.w(f"{E}{n}A")
            for k, o in enumerate(options):
                mark = ("■ " if on[k] else "□ ") if multi else ""
                here = k == i
                self.w(f"\r{E}2K      {self.pink + '›' if here else ' '}{self.reset} "
                       + (f"{self.sel} {mark}{o} {self.reset}" if here else f" {self.dim}{mark}{o}{self.reset}") + "\n")
            c = self.key()
            if c in ("\x1b[B", "\x1b[C", "\t", "j"):
                i = (i + 1) % n
            elif c in ("\x1b[A", "\x1b[D", "k"):
                i = (i - 1) % n
            elif c == " " and multi:
                on[i] = not on[i]
            elif c in ("\r", "\n"):
                picked = [o for o, x in zip(options, on, strict=True) if x] if multi else options[i]
                self.w(f"{E}{n + 1}A")
                self.answered(question, (", ".join(picked) or "none") if multi else picked)
                self.w(f"{E}J")
                return picked

    def ask(self, question: str, default: str, check=None) -> str:
        """A line of text, the default shown until something is typed. Arrow keys are ignored, not echoed."""
        if not self.interactive:
            self.answered(question, default)
            return default
        while True:
            typed = ""
            while True:
                self.w(self.prompt(question) + f" {typed if typed else self.dim + default + self.reset}{self.E}K")
                k = self.key()
                if k in ("\r", "\n"):
                    break
                if k in ("\x7f", "\x08"):
                    typed = typed[:-1]
                elif len(k) == 1 and k.isprintable():
                    typed += k
            answer = typed.strip() or default
            problem = check(answer) if check else None
            if not problem:
                self.answered(question, answer)
                return answer
            self.w(self.prompt(question) + f" {self.red}{problem}{self.reset}{self.E}K")
            time.sleep(1.2)

    def confirm(self, question: str) -> bool:
        return self.choose(question, ["Yes", "No"]) == "Yes"


class Spinner:
    def __init__(self, ui: Ui, text):
        self.ui, self.text, self.halt = ui, text, threading.Event()
        self.t = threading.Thread(target=self.spin, daemon=True)

    def spin(self):
        frames, i, ui = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏", 0, self.ui
        while not self.halt.is_set():
            text = self.text() if callable(self.text) else self.text
            ui.w(f"\r{ui.E}2K    {ui.pink}{frames[i % 10]}{ui.reset}  {ui.dim}{text}{ui.reset}")
            i += 1
            time.sleep(0.08)
        ui.w(f"\r{ui.E}2K")

    def __enter__(self):
        if self.ui.tty:
            self.t.start()
        return self

    def __exit__(self, *a):
        self.halt.set()
        if self.t.is_alive():
            self.t.join()


class Installer:
    """Numbered steps: a progress bar while each runs, then a tick, what it did and how long it took."""

    def __init__(self, ui: Ui, total: int):
        self.ui, self.n, self.total, self.started = ui, 0, total, time.time()

    def step(self, title: str, fn, done: str | None = None):
        """Run fn, which returns the lines describing what it did (or a tuple of (lines, done text))."""
        ui = self.ui
        self.n += 1
        tag = f"{ui.dim}[{self.n}/{self.total}]{ui.reset}"
        bar_w, result, error = max(10, ui.width - 44), {}, []

        def work():
            try:
                result["v"] = fn()
            except Exception as e:                              # shown as a failed step, then raised again
                error.append(e)
        t0 = time.time()
        th = threading.Thread(target=work, daemon=True)
        th.start()
        f = 0
        while th.is_alive() or (ui.tty and not ui.fast and f < 12):
            if ui.tty:
                filled = min(bar_w, f * bar_w // 12) if th.is_alive() or f < 12 else bar_w
                ui.w(f"\r{ui.E}2K    {tag} {title:<28}{ui.pink}{'━' * filled}{ui.dim}{'━' * (bar_w - filled)}{ui.reset}")
            f += 1
            th.join(0.05)
        took = f"{time.time() - t0:.1f}s"
        if error:
            ui.w(f"\r{ui.E}2K")
            ui.fail(f"{tag} {title}")
            raise error[0]
        v = result.get("v") or []
        lines, head = (v if isinstance(v, tuple) else (v, None))
        head = head or done or title
        pad = max(1, ui.width - 12 - len(ui.plain(head)) - len(took))
        ui.w(f"\r{ui.E}2K    {ui.green}✓{ui.reset} {tag} {head}{' ' * pad}{ui.dim}{took}{ui.reset}\n")
        for d in lines:
            if d.startswith(("/", "~", str(Path.home()))) and " " not in d:      # a path: shortened, never broken
                ui.line(f"            {ui.dim}{ui.fit(d, ui.width - 12)}{ui.reset}", 0.02)
                continue
            for part in textwrap.wrap(ui.fit(d, 400) if str(Path.home()) in d else d, ui.width - 12):
                ui.line(f"            {ui.dim}{part}{ui.reset}", 0.02)

    def finished(self, what: str) -> None:
        self.ui.line()
        self.ui.line(f"  {self.ui.green}{self.ui.bold}{what}{self.ui.reset} {self.ui.dim}in {time.time() - self.started:.1f}s{self.ui.reset}")


# -- the computer --------------------------------------------------------------------------------------------------
def network_shares() -> list[str]:
    """Mount points of network file systems: NFS, SMB and AFP."""
    try:
        out = subprocess.run(["mount"], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    found = []
    for line in out.splitlines():
        m = re.search(r" on (\S+) (?:type (\w+)|\((\w+)[,)])", line)
        if m and (m.group(2) or m.group(3) or "").lower() in ("nfs", "nfs4", "cifs", "smbfs", "smb3", "afpfs"):
            found.append(m.group(1))
    return found


def on_battery_power_device() -> bool:
    """True for a laptop: a computer with a battery."""
    if list(Path("/sys/class/power_supply").glob("BAT*")):
        return True
    try:
        out = subprocess.run(["pmset", "-g", "batt"], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return "InternalBattery" in out


def instruments() -> list[dict]:
    """What a rat could serve: displays (framebuffers) and USB devices a catalog profile fits, with their config."""
    found = []
    for fb in sorted(Path("/dev").glob("fb[0-9]*")):
        size = Path(f"/sys/class/graphics/{fb.name}/virtual_size")
        what = size.read_text().strip().replace(",", " x ") if size.is_file() else "display"
        found.append({"name": "slm" if fb.name == "fb0" else f"display{fb.name[2:]}", "what": f"Display {fb}, {what}",
                      "config": {"driver": "display", "framebuffer": str(fb)}})
    if platform.system() in ("Linux", "Darwin"):
        try:
            from .detect import scan
            for row in scan():
                if row.get("profiles") and row.get("files") and not row.get("in_use"):
                    p = row["profiles"][0]
                    base = re.sub(r"[^a-z0-9]+", "-", p["profile"].split("/")[-1].lower()).strip("-")[:20] or "device"
                    found.append({"name": base, "what": f"USB {p['model']}  ({p['profile']})",
                                  "config": {"profile": p["profile"], "port": row["files"][0]}})
        except Exception:
            pass
    return found


def computer() -> dict:
    from .worker import gpus, memory_gb
    return {"cpus": os.cpu_count() or 1, "memory_gb": memory_gb(), "gpus": gpus(), "shares": network_shares(),
            "devices": instruments(), "laptop": on_battery_power_device(), "host": socket.gethostname().split(".")[0]}


# -- the nest --------------------------------------------------------------------------------------------------------
def find_nests(seconds: float = 2.5) -> list:
    try:
        from .discovery import scan
        return [f for f in scan(seconds) if f.role == "nest"]
    except Exception:
        return []


def post(url: str, body: dict, timeout: float = 15.0) -> dict:
    req = urllib.request.Request(url.rstrip("/") + "/api/v1/join", data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            reply = json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            reply = json.loads(e.read())
        except ValueError:
            raise LabratError("unavailable", f"{url} answered HTTP {e.code}") from None
    except (OSError, ValueError) as e:
        raise LabratError("unavailable", f"cannot reach the nest at {url}: {e}") from None
    if not reply.get("ok"):
        err = reply.get("error") or {}
        raise LabratError(err.get("code", "internal"), err.get("message", "no reason given"))
    return reply["value"]


# -- where things go -------------------------------------------------------------------------------------------------
class Places:
    """Configuration, state and services: system-wide as root, the user's own otherwise."""

    def __init__(self, root: Path | None = None):
        self.system = os.geteuid() == 0 and root is None
        self.mac = platform.system() == "Darwin"
        if root is not None:                                   # tests and --root: everything under one folder
            self.conf, self.state, self.services = root / "config", root / "state", root / "services"
        elif self.system:
            self.conf, self.state, self.services = Path("/etc/labrat"), Path("/var/lib/labrat"), Path("/etc/systemd/system")
        else:
            self.conf, self.state = conf.default_paths("worker")[1].parent, conf.default_state_dir()
            self.services = (Path.home() / "Library/LaunchAgents") if self.mac else \
                Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "systemd/user"
        self.record = self.state / "setup.json"

    def config(self, kind: str) -> Path:
        return self.conf / f"{kind}.toml"

    def service_file(self, kind: str) -> Path:
        return self.services / (f"org.labrat.{kind}.plist" if self.mac and not self.system else f"labrat-{kind}.service")


def toml_value(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, list):
        return "[" + ", ".join(toml_value(x) for x in v) + "]"
    return json.dumps(str(v))


def write_private(path: Path, text: str, mode: int = 0o640) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.replace(tmp, path)


def labrat_command(kind: str, config: Path) -> list[str]:
    return [sys.executable, "-m", "labrat.cli", kind, "--config", str(config)]


def service_text(places: Places, kind: str) -> str:
    cmd = labrat_command(kind, places.config(kind))
    if places.mac and not places.system:
        logs = Path.home() / "Library/Logs/labrat"
        args = "".join(f"\n    <string>{a}</string>" for a in cmd)
        return f'''<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>org.labrat.{kind}</string>
  <key>ProgramArguments</key>
  <array>{args}
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>{logs}/{kind}.log</string>
  <key>StandardErrorPath</key><string>{logs}/{kind}.log</string>
</dict>
</plist>
'''
    if places.system:
        from .cli import UNIT
        groups = {"rat": "dialout gpio video plugdev", "worker": "video render"}[kind]
        return UNIT.format(kind=kind, exe=" ".join(cmd[:3]), groups=groups).replace(f"--config /etc/labrat/{kind}.toml", f"--config {places.config(kind)}")
    return f"""[Unit]
Description=LABRAT {kind}
After=network-online.target

[Service]
ExecStart={' '.join(cmd)}
Restart=on-failure
RestartSec=3

[Install]
WantedBy=default.target
"""


def start_service(places: Places, kind: str, dry: bool = False) -> list[str]:
    """Install and start one service. Returns what it did, in words."""
    path = places.service_file(kind)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(service_text(places, kind))
    if dry:
        return [str(path)]
    if places.mac and not places.system:
        (Path.home() / "Library/Logs/labrat").mkdir(parents=True, exist_ok=True)
        target = f"gui/{os.getuid()}"
        subprocess.run(["launchctl", "bootout", f"{target}/org.labrat.{kind}"], capture_output=True)
        run(["launchctl", "bootstrap", target, str(path)])
        return [str(path), "starts at login, restarts if it stops", f"logs in ~/Library/Logs/labrat/{kind}.log"]
    user = [] if places.system else ["--user"]
    run(["systemctl", *user, "daemon-reload"])
    run(["systemctl", *user, "enable", "--now", f"labrat-{kind}"])
    run(["systemctl", *user, "restart", f"labrat-{kind}"])
    return [str(path), "starts at boot, restarts if it stops" if places.system else
            "starts at login, restarts if it stops (loginctl enable-linger keeps it running when logged out)",
            f"logs: journalctl {'--user ' if user else ''}-u labrat-{kind}"]


def stop_service(places: Places, kind: str) -> None:
    path = places.service_file(kind)
    if places.mac and not places.system:
        subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/org.labrat.{kind}"], capture_output=True)
    elif shutil.which("systemctl"):
        user = [] if places.system else ["--user"]
        subprocess.run(["systemctl", *user, "disable", "--now", f"labrat-{kind}"], capture_output=True)
    path.unlink(missing_ok=True)


def run(cmd: list[str]) -> None:
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode:
        raise LabratError("internal", f"{' '.join(cmd)}: {(r.stderr or r.stdout).strip()[:300]}")


# -- the whole thing -------------------------------------------------------------------------------------------------
def setup(a) -> int:
    import logging
    logging.getLogger("labrat").setLevel(logging.ERROR)        # setup says what it does; log lines would break the screen
    places = Places(Path(a.root) if getattr(a, "root", None) else None)
    ui = Ui(interactive=not a.yes, fast=a.fast)
    if a.undo:
        return undo(ui, places)
    ui.header("Join this computer to a lab")
    try:
        return _setup(a, ui, places)
    except KeyboardInterrupt:
        ui.w(f"\r{ui.E}2K\n")
        ui.line(f"{ui.dim}Stopped. Anything already written is listed by labrat setup --undo, which removes it.{ui.reset}")
        ui.line()
        return 130
    finally:
        ui.w(f"{ui.E}?25h" if ui.tty else "")


def _setup(a, ui: Ui, places: Places) -> int:
    ui.w(f"{ui.E}?25l" if ui.tty else "")
    ui.section("This computer")
    with Spinner(ui, "Reading CPUs, memory, GPUs, shares and devices"):
        pc = computer()
    ui.row("Processor", f"{pc['cpus']} cores")
    if pc["memory_gb"]:
        ui.row("Memory", f"{pc['memory_gb']:g} GB")
    for g in pc["gpus"] or [None]:
        ui.row("GPU", f"{g['name']}, {g['memory_gb']:g} GB" if g else f"{ui.dim}none found{ui.reset}")
    ui.row("Network shares", ", ".join(pc["shares"]) or f"{ui.dim}none mounted{ui.reset}")
    ui.row("Devices", ", ".join(d["what"] for d in pc["devices"]) or f"{ui.dim}none found{ui.reset}")

    ui.section("Lab")
    url = a.url or os.environ.get("LABRAT_URL")
    if not url:
        with Spinner(ui, "Looking for a nest on this network"):
            nests = find_nests()
        if len(nests) == 1:
            url = nests[0].url
            ui.row("Nest", f"{nests[0].name}  {ui.dim}{url}{ui.reset}")
        elif nests:
            pick = ui.choose("Which nest?", [f"{n.name}  {n.url}" for n in nests])
            url = pick.split()[-1]
        else:
            ui.note("No nest answered on this network. Give its address, such as http://nest.local:8780.")
            url = ui.ask("Nest address", f"http://nest.local:{conf.NEST_PORT}")
    else:
        ui.row("Nest", url)
    from .worker import check_url
    check_url(url, bool(getattr(a, "allow_http", False)))

    ui.section("Configuration")
    join_key = a.key
    if not join_key and not a.yes and not ui.confirm("Join this nest?"):
        ui.line()
        ui.line(f"{ui.dim}Nothing was changed.{ui.reset}")
        return 1
    roles_default = ["Worker", "Rat", "Both"].index({"worker": "Worker", "rat": "Rat", "both": "Both"}[a.role]) if a.role else (2 if pc["devices"] else 0)
    role = ui.choose("Role", ["Worker", "Rat", "Both"], roles_default).lower()
    roles = ["worker", "rat"] if role == "both" else [role]
    devices = []
    if "rat" in roles:
        if pc["devices"]:
            chosen = ui.choose("Serve as a rat", [d["what"] for d in pc["devices"]], multi=True)
            devices = [d for d in pc["devices"] if d["what"] in chosen]
        else:
            ui.note("No devices were found, so the rat starts with none. Add them later with labrat add.")
    cfg: dict = {}
    if "worker" in roles:
        shared = a.shared or (not join_key and ui.choose("Available to", ["Only me", "The whole lab"]) == "The whole lab")
        whens = ["When idle", "Always", "In set hours"]
        when = {"idle": 0, "always": 1}.get(a.when or "", 0 if pc["laptop"] else 1)
        when = ui.choose("Take jobs", whens, 2 if a.hours else when)
        cfg["shared"] = bool(shared)
        if when == "When idle":
            cfg["when"] = "idle"
        elif when == "In set hours":
            from .worker import hours as check_hours
            cfg["hours"] = a.hours or ui.ask("Hours", "18:00-07:00", lambda v: _problem(check_hours, v))
        cpus = a.cpus or ui.ask("CPU limit", str(pc["cpus"]), lambda v: None if v.isdigit() and int(v) > 0 else "a whole number")
        if int(cpus) < pc["cpus"]:
            cfg["cpus"] = int(cpus)
        stores = dict(s.split("=", 1) for s in a.store or [])
        for share in pc["shares"] if not a.store else []:
            name = ui.ask(f"Store at {Path(share).name}", "skip", lambda v: None if v == "skip" else _problem(conf.check_name, v, "store"))
            if name != "skip":
                stores[name] = share
        cfg["stores"] = stores
    name = a.name or ui.ask("Computer name", pc["host"], lambda v: _problem(conf.check_name, v, "computer"))

    summary = [("Name", name), ("Nest", url), ("Role", " and ".join(roles))]
    if "worker" in roles:
        summary += [("Available to", "the lab, once approved" if cfg["shared"] else "its owner"),
                    ("Jobs", ("when idle" if cfg.get("when") == "idle" else f"from {cfg['hours']}" if cfg.get("hours") else "any time")
                     + (f", up to {cfg['cpus']} CPUs" if cfg.get("cpus") else "")),
                    ("Stores", ", ".join(f"{k} at {v}" for k, v in cfg["stores"].items()) or "none")]
    if "rat" in roles:
        summary.append(("Rat devices", ", ".join(d["name"] for d in devices) or "none yet"))
    summary += [("Approval", "join key" if join_key else "a person approves a code"),
                ("Configuration", str(places.conf)), ("Services", "none" if a.no_service else
                                                       "launchd" if places.mac and not places.system else "systemd" + ("" if places.system else " (user)"))]
    ui.section("Summary")
    for k, v in summary:
        ui.row(k, v)
    if "rat" in roles and a.no_service:
        ui.line()
        ui.note(f"With --no-service nothing starts the rat, and the nest adopts it only if it answers. Start it "
                f"with: labrat rat --config {ui.fit(places.config('rat'), 200)}, before the code is approved.")
    if a.dry_run:
        ui.line()
        ui.line(f"{ui.dim}Dry run: nothing was written, joined or started.{ui.reset}")
        ui.line()
        return 0
    if not a.yes:
        ui.line()
        if not ui.confirm("Apply these settings?"):
            ui.line()
            ui.line(f"{ui.dim}Nothing was changed.{ui.reset}")
            return 1
    return apply(a, ui, places, url, name, roles, devices, cfg, join_key)


def _problem(check, *args) -> str | None:
    try:
        check(*args)
        return None
    except LabratError as e:
        return e.message


def apply(a, ui: Ui, places: Places, url: str, name: str, roles: list, devices: list, cfg: dict, join_key: str | None) -> int:
    from .rat import fingerprint
    from .rat import load_or_create_key as rat_key
    from .worker import load_or_create_key as worker_key
    record = {"nest": url, "name": name, "roles": roles, "files": [], "services": [], "system": places.system}

    def save_record():
        write_private(places.record, json.dumps(record, indent=2) + "\n", 0o600)

    keys: dict[str, str] = {}
    ui.section("Setting up")
    first = Installer(ui, len(roles) + ("rat" in roles and not a.no_service))
    if "worker" in roles:
        def write_worker():
            lines = [f'name = "{name}"', f'nest = "{url}"', f'state_dir = "{places.state}"']
            lines += [f"{k} = {toml_value(cfg[k])}" for k in ("shared", "when", "hours", "cpus") if k in cfg]
            for s, path in cfg.get("stores", {}).items():
                lines += ["", f"[stores.{s}]", f'path = "{path}"']
            write_private(places.config("worker"), "# Written by labrat setup. Reference: docs/workers.md\n" + "\n".join(lines) + "\n")
            record["files"].append(str(places.config("worker")))
            keys["worker"] = worker_key(places.state)
            record["files"].append(str(places.state / "worker.key"))
            save_record()
            return ([str(places.config("worker")), f"{places.state / 'worker.key'}, readable by you only",
                     f"fingerprint {fingerprint(keys['worker'])}"], "Worker configured, key made")
        first.step("Configure the worker", write_worker)
    if "rat" in roles:
        def write_rat():
            lines = [f'name = "{name}"', f"port = {conf.RAT_PORT}", f'state_dir = "{places.state}"', "watchdog_s = 30", "discovery = true"]
            for d in devices:
                lines += ["", f"[devices.{d['name']}]"] + [f"{k} = {toml_value(v)}" for k, v in d["config"].items()]
            write_private(places.config("rat"), "# Written by labrat setup. Reference: docs/configuration.md\n" + "\n".join(lines) + "\n")
            record["files"].append(str(places.config("rat")))
            keys["rat"] = rat_key(places.state)
            record["files"].append(str(places.state / "rat.key"))
            save_record()
            return ([str(places.config("rat")), f"devices: {', '.join(d['name'] for d in devices) or 'none yet'}",
                     "watchdog: devices go safe if the nest goes quiet", f"fingerprint {fingerprint(keys['rat'])}"], "Rat configured, key made")
        first.step("Configure the rat", write_rat)
        if not a.no_service:
            def rat_up():
                lines = start_service(places, "rat")
                record["services"].append("rat")
                save_record()
                wait_for(f"http://127.0.0.1:{conf.RAT_PORT}/api/v1/health", 20)
                return lines, "Rat running"
            first.step("Start the rat", rat_up)

    ui.section("Joining")
    from .rat import fingerprint as fp
    shown = fp(keys.get("worker") or keys["rat"])
    ask = {"action": "ask", "name": name, "roles": roles, "shared": bool(cfg.get("shared")),
           **{f"{r}_key": keys[r] for r in roles}, **({"rat_port": conf.RAT_PORT} if "rat" in roles else {}),
           "summary": {"cpus": os.cpu_count(), "platform": platform.system()}, **({"join_key": join_key} if join_key else {})}
    with Spinner(ui, "Asking the nest"):
        req = post(url, ask)
    if req["state"] != "approved":
        from .joins import proof
        ui.line()
        ui.panel("Approve this computer", ["", f"Code           {ui.yellow}{ui.bold}{req['code']}{ui.reset}",
                                           f"Fingerprint    {shown}", "", f"{ui.dim}On a signed-in computer, run:{ui.reset}",
                                           f"labrat approve {req['code']}", ""])
        ui.line()
        end = time.time() + int(req.get("expires_s", 600))
        p = proof(keys.get("worker") or keys["rat"], req["id"])

        def left():
            s = max(0, int(end - time.time()))
            return f"Waiting for approval  (the code runs out in {s // 60}:{s % 60:02d})"
        with Spinner(ui, left):
            while req["state"] == "waiting" and time.time() < end:
                time.sleep(2)
                try:
                    req = post(url, {"action": "status", "id": req["id"], "proof": p})
                except LabratError:
                    continue
        if req["state"] != "approved":
            ui.fail({"denied": f"Denied by {req.get('decided_by', 'a person')}", "failed": f"Not joined: {req.get('error', '')}"}
                    .get(req["state"], "The code ran out before anyone approved it"))
            ui.note("Run labrat setup again to ask again, or labrat setup --undo to remove what was written.")
            ui.line()
            return 2
    ui.ok(f"Approved by {req.get('decided_by', 'the nest')}: {name} is {req['owner']}'s"
          + (", shared with the lab" if req.get("shared") else "") + f" {' and '.join(roles)}")
    record["owner"] = req["owner"]
    save_record()

    if "worker" in roles:
        ui.section("Installing")
        inst = Installer(ui, 2 if not a.no_service else 1)

        def joined():
            write_private(places.state / "worker.json", json.dumps({"nest": url, "name": name, "owner": req["owner"],
                                                                     "shared": "yes" if req.get("shared") else "no"}, indent=2), 0o600)
            record["files"].append(str(places.state / "worker.json"))
            save_record()
            stores = cfg.get("stores") or {}
            return ([f"owner {req['owner']}, {'shared with the lab' if req.get('shared') else 'personal'}",
                     "requests signed with this computer's key; the key never leaves it",
                     *([f"stores: {', '.join(f'{k} -> {v}' for k, v in stores.items())}"] if stores else [])], "Registered with the nest")
        inst.step("Register the worker", joined)
        if not a.no_service:
            def worker_up():
                lines = start_service(places, "worker")
                record["services"].append("worker")
                save_record()
                return lines, "Worker running"
            inst.step("Start the worker", worker_up)
        inst.finished("Installed")
    if places.system:
        _give_to_labrat_user(places)
    ui.section("Next")
    nxt = [("labrat workers", "see it in the lab")] if "worker" in roles else []
    nxt += [("labrat devices", "its devices")] if "rat" in roles else []
    nxt += [("labrat setup --undo", "remove it again")]
    width = max(ui.label - 2, max(len(c) for c, _ in nxt) + 3)
    for c, what in nxt:
        ui.line(f"  {c:<{width}}{ui.dim}{what}{ui.reset}", 0.03)
    ui.line()
    ui.rule()
    ui.line()
    return 0


def wait_for(url: str, seconds: float) -> None:
    end = time.time() + seconds
    while time.time() < end:
        try:
            with urllib.request.urlopen(url, timeout=2):
                return
        except OSError:
            time.sleep(0.5)
    raise LabratError("unavailable", f"the rat did not answer at {url} within {seconds:g} s")


def _give_to_labrat_user(places: Places) -> None:
    """As root, the services run as the labrat user: it owns the state folder and reads the configuration."""
    import grp
    import pwd
    try:
        u, g = pwd.getpwnam("labrat").pw_uid, grp.getgrnam("labrat").gr_gid
    except KeyError:
        return
    for p in [places.state, *places.state.rglob("*")]:
        os.chown(p, u, g)
    for kind in ("worker", "rat"):
        if places.config(kind).is_file():
            os.chown(places.config(kind), 0, g)


def undo(ui: Ui, places: Places) -> int:
    ui.header("Remove this computer from its lab")
    if not places.record.is_file():
        ui.line(f"{ui.dim}labrat setup has not set this computer up; nothing to remove.{ui.reset}")
        ui.line()
        return 0
    rec = json.loads(places.record.read_text())
    ui.section("Removing")
    ui.row("Computer", rec["name"])
    ui.row("Nest", rec["nest"])
    ui.line()
    if ui.interactive and not ui.confirm("Remove it?"):
        return 1
    if "worker" in rec["roles"] and (places.state / "worker.json").is_file():
        try:
            from .worker import Worker, load_config
            Worker(load_config(places.config("worker"))).post("leave")
            ui.ok("Left the nest's workers")
        except (LabratError, OSError) as e:
            ui.fail(f"Could not tell the nest ({getattr(e, 'message', e)}); an admin can run labrat worker remove {rec['name']}")
    if "rat" in rec["roles"]:
        ui.note(f"The nest still lists the rat; an admin removes it with labrat forget {rec['name']}.")
    for kind in rec.get("services", []):
        stop_service(places, kind)
        ui.ok(f"Service labrat-{kind} stopped and removed")
    for f in reversed(rec.get("files", [])):
        Path(f).unlink(missing_ok=True)
    ui.ok(f"Removed {len(rec.get('files', []))} files written by setup")
    places.record.unlink(missing_ok=True)
    ui.line()
    return 0

