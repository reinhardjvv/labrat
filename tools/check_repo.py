#!/usr/bin/env python3
"""Check the repository for private data and for wording that does not belong in it.

    python tools/check_repo.py            # every tracked file, or every file when not in git

Fails on real IP addresses (documentation ranges are fine), home paths, e-mail addresses, things that look
like secrets, em dashes and promotional filler. Words from your own lab that must never be published go in an
untracked file, one regular expression per line, named by LABRAT_PRIVATE_WORDS or kept as .private-words in the
repository root (git ignores it).
"""
from __future__ import annotations

import ipaddress
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKIP_FILES = {"LICENSE", "tools/check_repo.py"}
SKIP_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".svg", ".pdf", ".whl", ".gz"}
# documentation ranges, loopback, and NetworkManager's default for a shared network (labrat network)
ALLOWED_NETS = [ipaddress.ip_network(n) for n in ("192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24", "127.0.0.0/8", "0.0.0.0/32",
                                                  "10.42.0.0/24")]


def private_words() -> list[str]:
    import os
    path = Path(os.environ.get("LABRAT_PRIVATE_WORDS") or ROOT / ".private-words")
    if not path.is_file():
        return []
    return [w.strip() for w in path.read_text().splitlines() if w.strip() and not w.startswith("#")]


PRIVATE = private_words()
FILLER = ["delve", "seamless", "leverage", "cutting-edge", "cutting edge", "game-changer", "game changer",
          "supercharge", "effortless", "empower", "elevate your", "unlock the power", "in today's",
          "it's worth noting", "it is worth noting", "best-in-class", "world-class", "revolutionise", "revolutionize"]

CHECKS = [
    ("IP address", re.compile(r"(?<![\w.])(\d{1,3}(?:\.\d{1,3}){3})(?![\w.])")),
    ("home path", re.compile(r"/(?:home|Users)/[A-Za-z0-9_.-]+")),
    ("e-mail address", re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}\b")),
    ("secret", re.compile(r"sk-ant-[A-Za-z0-9_-]{8,}|ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|AKIA[0-9A-Z]{16}|-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("em dash", re.compile("—")),
    ("filler", re.compile(r"\b(" + "|".join(map(re.escape, FILLER)) + r")", re.I)),
]
if PRIVATE:
    CHECKS.append(("private word", re.compile("(" + "|".join(PRIVATE) + ")", re.I)))
EMAIL_OK = re.compile(r"(noreply|example)", re.I)


def files() -> list[Path]:
    try:
        out = subprocess.run(["git", "ls-files", "-co", "--exclude-standard"], cwd=ROOT, capture_output=True, text=True, check=True).stdout
        names = out.split()
    except (OSError, subprocess.CalledProcessError):
        names = [str(p.relative_to(ROOT)) for p in ROOT.rglob("*") if p.is_file() and ".venv" not in p.parts and ".git" not in p.parts]
    venvs = {c.parent.name for c in ROOT.glob("*/pyvenv.cfg")}                 # virtual environments, whatever their name
    return [ROOT / n for n in names if n not in SKIP_FILES and Path(n).suffix.lower() not in SKIP_SUFFIXES
            and Path(n).parts[0] not in venvs and (ROOT / n).is_file() and not (ROOT / n).is_symlink()]


def problems(path: Path) -> list[str]:
    try:
        text = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, FileNotFoundError):
        return []
    found = []
    for n, line in enumerate(text.splitlines(), 1):
        for what, rx in CHECKS:
            for m in rx.finditer(line):
                hit = m.group(1) if rx.groups else m.group(0)
                if what == "IP address":
                    try:
                        ip = ipaddress.ip_address(hit)
                    except ValueError:
                        continue                                   # a version number or similar
                    if any(ip in net for net in ALLOWED_NETS):
                        continue
                if what == "e-mail address" and EMAIL_OK.search(hit):
                    continue
                found.append(f"{path.relative_to(ROOT)}:{n}: {what}: {hit}")
    return found


def main() -> int:
    found = [p for f in files() for p in problems(f)]
    for p in found:
        print(p)
    print(f"{len(found)} problem(s)" if found else "clean")
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
