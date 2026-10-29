"""The labrat command."""
from __future__ import annotations

import argparse
import json
import logging
import os
import secrets
import signal
import socket
import sys
import tempfile
import threading
from pathlib import Path

from . import __version__
from . import config as conf
from .client import Client
from .hub import token_hash
from .message import ROLES, LabratError

log = logging.getLogger("labrat")

UNIT = """[Unit]
Description=LABRAT {kind}
Documentation=https://github.com/reinhardjvv/labrat
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
User=labrat
Group=labrat
SupplementaryGroups={groups}
ExecStart={exe} {kind} --config /etc/labrat/{kind}.toml
Restart=on-failure
RestartSec=3
StateDirectory=labrat
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes

[Install]
WantedBy=multi-user.target
"""


def _token_entry(name: str, role: str) -> tuple[str, str]:
    token = secrets.token_urlsafe(24)
    return token, f'[[tokens]]\nname = "{name}"\nrole = "{role}"\nsha256 = "{token_hash(token)}"\n'


def _write_new(path: Path, text: str) -> None:
    if path.exists():
        raise LabratError("bad_request", f"{path} exists; not overwriting it")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o640)
    with os.fdopen(fd, "w") as f:
        f.write(text)


def _state_line() -> str:
    """A state_dir line when the service's state directory is known, so every user reads the same keys."""
    d = os.environ.get("STATE_DIRECTORY")
    return f'state_dir = "{d}"\n' if d else ""


def _default_new_path(kind: str) -> Path:
    return conf.default_paths(kind)[0 if os.geteuid() == 0 else 1]


# -- running a nest or a rat -------------------------------------------------------------------------------------
def serve(kind: str, cfg: dict, hub_factory) -> int:
    from .discovery import Advertiser
    from .http import Server

    if kind == "nest" and not cfg.get("tokens") and not conf.is_loopback(cfg["host"]):
        raise LabratError("bad_request", "a nest on the network needs at least one [[tokens]] entry (labrat token)")
    hub = hub_factory(cfg)
    server = Server(hub, cfg["host"], int(cfg["port"])).start()
    ad = Advertiser(kind, hub.name, server.port, getattr(hub, "fingerprint", "")) if cfg.get("discovery") else None
    log.info("%s %s serving %d device(s) on %s:%d", kind, hub.name, len(hub.devices), cfg["host"], server.port)
    for d in hub.devices.values():
        if not d.safe and any(a.access == "rw" for a in d.attrs.values()) and not getattr(d, "safe_commands", None):
            log.warning("%s can be changed but has no safe state: stop and the watchdog leave it as it is", d.name)
    if kind == "rat":
        log.info("rat key fingerprint %s (labrat rat key shows the key)", hub.fingerprint)
    done = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: done.set())
    done.wait()
    log.info("shutting down: putting every device in its safe state")
    if ad:
        ad.close()
    server.close()
    hub.shutdown()
    hub.close()
    return 0


def cmd_nest(a) -> int:
    from .nest import Nest
    if a.action == "init":
        path = Path(a.config).expanduser() if a.config else _default_new_path("nest")
        token, entry = _token_entry(a.admin, "admin")
        name = socket.gethostname().split(".")[0] + "-nest"
        _write_new(path, f"# LABRAT nest configuration. Reference: docs/configuration.md\nname = \"{name}\"\nport = {conf.NEST_PORT}\n{_state_line()}\n"
                         f"{entry}\n# Devices plugged into the nest itself, if any:\n# [devices.psu]\n# driver = \"sim-supply\"\n")
        print(f"wrote {path}\n\nadmin token for {a.admin} (shown once, keep it safe):\n  {token}\n\n"
              f"export LABRAT_URL=http://{socket.gethostname()}:{conf.NEST_PORT}\nexport LABRAT_TOKEN={token}")
        return 0
    return serve("nest", conf.load("nest", conf.find("nest", a.config)), Nest)


def cmd_rat(a) -> int:
    from .detect import rat_config, resources
    from .rat import Rat, fingerprint, key_path, load_or_create_key
    if a.action == "init":
        path = Path(a.config).expanduser() if a.config else _default_new_path("rat")
        _write_new(path, rat_config(socket.gethostname().split(".")[0], _state_line()))
        print(f"wrote {path}; edit it to add the devices on this rat. Found:")
        for kind, items in resources().items():
            print(f"  {kind}: {', '.join(items) or 'none'}")
        return 0
    if a.action == "key":
        cfg = conf.load("rat", conf.find("rat", a.config))
        if not key_path(cfg["state_dir"]).is_file():
            raise LabratError("not_found", f"no key in {cfg['state_dir']} yet; the rat makes one when it first starts")
        key = load_or_create_key(cfg["state_dir"])
        print(f"rat          {cfg['name']}\nkey          {key}\nfingerprint  {fingerprint(key)}\nstored in    {key_path(cfg['state_dir'])}\n\n"
              f"to adopt it: labrat adopt {cfg['name']}   (asks for the key, or reads LABRAT_RAT_KEY)")
        return 0
    return serve("rat", conf.load("rat", conf.find("rat", a.config)), Rat)


def cmd_demo(a) -> int:
    """A nest on this computer with simulated devices, to try everything without hardware."""
    from .nest import Nest
    token = secrets.token_urlsafe(16)
    state = Path(tempfile.mkdtemp(prefix="labrat-demo-"))
    path = state / "nest.toml"
    path.write_text(f'name = "demo-nest"\nhost = "127.0.0.1"\nport = {a.port}\ndiscovery = false\nstate_dir = "{state}"\n'
                    f'[[tokens]]\nname = "demo"\nrole = "admin"\nsha256 = "{token_hash(token)}"\n'
                    '[devices.psu]\ndriver = "sim-supply"\ndescription = "simulated two-channel supply"\n'
                    '[devices.psu.limits.VoltageLevel]\nmax = 12\n'
                    '[devices.relays]\ndriver = "sim-gpio"\noutputs = { heater = 17, lamp = 27 }\ninputs = { door = 22 }\n'
                    'safe = { heater = false }\n')
    print(f"demo nest on http://127.0.0.1:{a.port}, simulated devices psu and relays\n\n"
          f"export LABRAT_URL=http://127.0.0.1:{a.port}\nexport LABRAT_TOKEN={token}\nlabrat devices\n"
          "labrat set psu VoltageLevel 5 --channel 1\n\nCtrl-C stops it.\n", flush=True)
    return serve("nest", conf.load("nest", path), Nest)


# -- talking to a nest or a rat ----------------------------------------------------------------------------------
def _client(a) -> Client:
    return Client(a.url, a.token)


def _show(value) -> None:
    print(value if isinstance(value, str) else json.dumps(value, indent=2, default=str))


def _value(text: str):
    try:
        return json.loads(text)
    except ValueError:
        return text


def cmd_devices(a):
    rows = _client(a).devices()
    if a.json:
        return _show(rows)
    for r in rows:
        lock = f"  locked by {r['locked_by']}" if r.get("locked_by") else ""
        state = "  (offline)" if r.get("online") is False else ""
        print(f"{r['device']:<28} {r.get('kind', ''):<14} {r.get('description', '')}{lock}{state}")


def cmd_find(a):
    from .discovery import scan
    found = scan(a.seconds)
    if not found:
        print("nothing found (is multicast DNS allowed on this network?)")
    for f in found:
        print(f"{f.role:<5} {f.name:<24} {f.url:<28} {f.version:<8} {f.fingerprint}")


def cmd_adopt(a):
    import getpass
    key = os.environ.get("LABRAT_RAT_KEY") or getpass.getpass(f"key of rat {a.name}: ")   # kept out of shell history
    _show(_client(a).adopt(a.name, key.strip(), a.at))


def cmd_token(a):
    token, entry = _token_entry(a.name, a.role)
    print(f"token for {a.name} ({a.role}), shown once:\n  {token}\n\nadd to the nest or rat configuration:\n\n{entry}")


def _group_exists(grp, name: str) -> bool:
    try:
        grp.getgrnam(name)
        return True
    except KeyError:
        return False


def cmd_service(a):
    exe = Path(sys.argv[0]).resolve() if Path(sys.argv[0]).name == "labrat" else Path(sys.executable).with_name("labrat")
    import grp
    wanted = ("dialout", "gpio", "video", "plugdev") if a.kind == "rat" else ()
    groups = " ".join(g for g in wanted if _group_exists(grp, g))
    print(UNIT.format(kind=a.kind, exe=exe, groups=groups).replace("SupplementaryGroups=\n", ""), end="")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="labrat", description="One safe message interface to lab equipment.")
    p.add_argument("--version", action="version", version=f"labrat {__version__}")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("nest", help="run the nest (the lab's server), or 'nest init' to make its configuration")
    s.add_argument("action", nargs="?", choices=["run", "init"], default="run")
    s.add_argument("--config")
    s.add_argument("--admin", default=os.environ.get("SUDO_USER") or os.environ.get("USER") or "admin",
                   help="name for the first admin token (init)")
    s.set_defaults(fn=cmd_nest)
    s = sub.add_parser("rat", help="run a rat, 'rat init' to make its configuration, 'rat key' to show its key")
    s.add_argument("action", nargs="?", choices=["run", "init", "key"], default="run")
    s.add_argument("--config")
    s.set_defaults(fn=cmd_rat)
    s = sub.add_parser("demo", help="a nest with simulated devices on this computer")
    s.add_argument("--port", type=int, default=conf.NEST_PORT)
    s.set_defaults(fn=cmd_demo)
    s = sub.add_parser("find", help="list the nests and rats on the network")
    s.add_argument("--seconds", type=float, default=3.0)
    s.set_defaults(fn=cmd_find)
    s = sub.add_parser("token", help="make a token and the line that grants it")
    s.add_argument("--name", required=True)
    s.add_argument("--role", choices=ROLES, default="operator")
    s.set_defaults(fn=cmd_token)
    s = sub.add_parser("service", help="print a systemd unit for a nest or rat")
    s.add_argument("kind", choices=["nest", "rat"])
    s.set_defaults(fn=cmd_service)
    s = sub.add_parser("drivers", help="the device drivers this installation has")
    s.set_defaults(fn=lambda a: [print(f"{k:<14} {v}") for k, v in sorted(__import__("labrat.drivers", fromlist=["available"]).available().items())])
    s = sub.add_parser("mcp", help="serve the lab to an AI agent over MCP (stdio)")
    s.set_defaults(fn=lambda a: __import__("labrat.mcp", fromlist=["run"]).run(_client(a)))

    def client_cmd(name, help_, fn, *args):
        c = sub.add_parser(name, help=help_)
        for arg in args:
            if isinstance(arg, tuple):
                c.add_argument(*arg[0], **arg[1])
            else:
                c.add_argument(arg)
        c.set_defaults(fn=fn)
        return c

    ch = (("--channel", "-c"), {})
    client_cmd("devices", "list the devices", cmd_devices, (("--json",), {"action": "store_true"}))
    client_cmd("describe", "what a device offers", lambda a: _show(_client(a).describe(a.device)), "device")
    client_cmd("get", "read an attribute", lambda a: _show(_client(a).get(a.device, a.name, a.channel)), "device", "name", ch)
    client_cmd("set", "set an attribute", lambda a: _show(_client(a).set(a.device, a.name, _value(a.value), a.channel)),
               "device", "name", "value", ch)
    client_cmd("call", "run a function, arguments as key=value",
               lambda a: _show(_client(a).call(a.device, a.name, a.channel, **{k: _value(v) for k, v in (x.split("=", 1) for x in a.args)})),
               "device", "name", (("args",), {"nargs": "*"}), ch)
    client_cmd("lock", "take a device", lambda a: _show(_client(a).lock(a.device, a.minutes)), "device",
               (("--minutes",), {"type": float}))
    client_cmd("unlock", "release a device", lambda a: _show(_client(a).unlock(a.device)), "device")
    client_cmd("stop", "put every device in its safe state", lambda a: _show(_client(a).stop(a.reason)),
               (("--reason",), {"default": "labrat stop"}))
    client_cmd("rats", "rats adopted and seen", lambda a: _show(_client(a).rats()))
    client_cmd("adopt", "adopt a rat; the key is asked for, or read from LABRAT_RAT_KEY", cmd_adopt, "name",
               (("--at",), {"help": "the rat's URL, when discovery cannot see it"}))
    client_cmd("forget", "forget an adopted rat", lambda a: _show(_client(a).forget(a.name)), "name")
    for c in sub.choices.values():
        c.add_argument("--url", default=None, help="nest or rat URL (default LABRAT_URL)")
        c.add_argument("--token", default=None, help="token (default LABRAT_TOKEN)")

    a = p.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        r = a.fn(a)
        return r if isinstance(r, int) else 0
    except LabratError as e:
        print(f"labrat: {e.code}: {e.message}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
