"""Configuration files (TOML) for the nest and the rats, with their defaults and checks."""
from __future__ import annotations

import os
import socket
import tomllib
from pathlib import Path

from .hub import Principal
from .message import ROLES, LabratError

NEST_PORT = 8780
RAT_PORT = 8781

COMMON = {"name", "host", "port", "state_dir", "discovery", "lock_minutes", "devices", "tokens", "approvals", "approval_minutes", "stores", "recordings",
          "catalog", "network"}
NEST_KEYS = COMMON | {"limits", "heartbeat_s", "experiments", "require_lock"}
RAT_KEYS = COMMON | {"watchdog_s", "allow_god_mode"}


def default_paths(kind: str) -> list[Path]:
    """Where a nest or rat looks for its configuration when none is given, in order."""
    home = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return [Path(f"/etc/labrat/{kind}.toml"), home / "labrat" / f"{kind}.toml"]


def default_state_dir() -> Path:
    if os.environ.get("STATE_DIRECTORY"):                     # set by systemd's StateDirectory=
        return Path(os.environ["STATE_DIRECTORY"].split(":")[0])
    if os.geteuid() == 0:
        return Path("/var/lib/labrat")
    return Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")) / "labrat"


def find(kind: str, given: str | None) -> Path:
    if given:
        p = Path(given).expanduser()
        if not p.is_file():
            raise LabratError("bad_request", f"no configuration file {p}")
        return p
    for p in default_paths(kind):
        if p.is_file():
            return p
    raise LabratError("bad_request", f"no {kind} configuration; run 'labrat {kind} init' or pass --config "
                      f"(looked in {', '.join(map(str, default_paths(kind)))})")


def load(kind: str, path: Path) -> dict:
    """The configuration with defaults filled in. Unknown keys are an error, so typing mistakes show up."""
    try:
        cfg = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as e:
        raise LabratError("bad_request", f"{path}: {e}") from None
    allowed = NEST_KEYS if kind == "nest" else RAT_KEYS
    unknown = set(cfg) - allowed
    if unknown:
        raise LabratError("bad_request", f"{path}: unknown setting(s) {', '.join(sorted(unknown))}")
    cfg.setdefault("name", socket.gethostname().split(".")[0] + ("-nest" if kind == "nest" else ""))
    cfg.setdefault("host", "0.0.0.0")
    cfg.setdefault("port", NEST_PORT if kind == "nest" else RAT_PORT)
    cfg.setdefault("discovery", True)
    cfg.setdefault("lock_minutes", 10.0)
    cfg.setdefault("devices", {})
    cfg["state_dir"] = Path(cfg.get("state_dir") or default_state_dir()).expanduser()
    cat = cfg.get("catalog", [str(cfg["state_dir"] / "profiles")])
    cfg["catalog"] = [Path(str(c)).expanduser() for c in ([cat] if isinstance(cat, str) else cat)]
    if kind == "nest":
        cfg.setdefault("heartbeat_s", 5.0)
        cfg.setdefault("limits", {})
    else:
        cfg.setdefault("watchdog_s", 30.0)
    if not isinstance(cfg["devices"], dict):
        raise LabratError("bad_request", f"{path}: devices is a table of [devices.<name>] tables")
    for name in cfg["devices"]:
        check_name(name, "device")
    check_name(cfg["name"], kind)
    if "network" in cfg:
        from .network import Network
        Network(cfg)                                          # its mistakes show at start, not only at 'network up'
    return cfg


def check_name(name: str, what: str) -> None:
    ok = name and len(name) <= 63 and all(c.isalnum() or c in "-_" for c in name) and name[0].isalnum()
    if not ok:
        raise LabratError("bad_request", f"{what} name {name!r}: use letters, digits, '-' and '_' (at most 63)")


def tokens(cfg: dict) -> dict[str, Principal]:
    """sha256 -> Principal from [[tokens]] entries: {name, role, sha256} and, for a token that works on a
    person's behalf (an AI agent's, say), for = "<person>"."""
    out = {}
    for t in cfg.get("tokens") or []:
        if not isinstance(t, dict) or not {"name", "role", "sha256"} <= set(t):
            raise LabratError("bad_request", "each [[tokens]] entry needs name, role and sha256")
        if set(t) - {"name", "role", "sha256", "for", "god_mode"}:
            raise LabratError("bad_request", f"token {t['name']}: settings are name, role, sha256, for and god_mode")
        god = t.get("god_mode", False)
        if not isinstance(god, bool):
            raise LabratError("bad_request", f"token {t['name']}: god_mode is true or false")
        if god and t["role"] == "viewer":
            raise LabratError("bad_request", f"token {t['name']}: god mode needs the operator or admin role")
        if t["role"] not in ROLES:
            raise LabratError("bad_request", f"token {t['name']}: role is one of {', '.join(ROLES)}")
        h = str(t["sha256"]).lower()
        if len(h) != 64 or any(c not in "0123456789abcdef" for c in h):
            raise LabratError("bad_request", f"token {t['name']}: sha256 is 64 hex digits (labrat token makes one)")
        person = str(t["for"]) if t.get("for") else None
        if person == str(t["name"]):
            person = None
        out[h] = Principal(str(t["name"]), str(t["role"]), person, god)
    return out


def is_loopback(host: str) -> bool:
    return host in ("127.0.0.1", "::1", "localhost")
