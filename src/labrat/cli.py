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
    if hub.recorder is not None:
        from .hub import Principal
        hub.recorder.autostart(Principal("recorder", "operator"))
    ad = Advertiser(kind, hub.name, server.port, getattr(hub, "fingerprint", "")) if cfg.get("discovery") else None
    log.info("%s %s serving %d device(s) on %s:%d", kind, hub.name, len(hub.devices), cfg["host"], server.port)
    for d in hub.devices.values():
        if not d.safe and any(a.access == "rw" for a in d.attrs.values()) and not getattr(d, "safe_commands", None):
            log.warning("%s can be changed but has no safe state: stop and the watchdog leave it as it is", d.name)
    for who in hub.tokens.values():
        if who.god:
            log.warning("token %s is in god mode: the lab's limits and approval rules do not apply to it", who.name)
    if kind == "rat":
        log.info("rat key fingerprint %s (labrat rat key shows the key)", hub.fingerprint)
        if getattr(hub, "allow_god_mode", False):
            log.warning("this rat lets the nest's god-mode tokens past its limits")
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
    if isinstance(value, str):
        print(value)
        return
    short = json.dumps(value, default=str)
    print(short if len(short) <= 100 else json.dumps(value, indent=2, default=str))


def _value(text: str):
    try:
        return json.loads(text)
    except ValueError:
        return text


def _st():
    from .term import Style, colour_wanted
    return Style(colour_wanted())


def _reply(a, msg: dict) -> dict:
    """The whole reply (value and unit), or the error raised as a LabratError."""
    r = _client(a).send({k: v for k, v in msg.items() if v is not None})
    if not r.get("ok"):
        err = r.get("error") or {}
        raise LabratError(err.get("code", "internal"), err.get("message", "no reason given"),
                          **{k: v for k, v in err.items() if k not in ("code", "message")})
    return r


def _print_value(r: dict, json_out: bool = False) -> None:
    from .term import quantity
    v = r.get("value")
    if json_out or isinstance(v, (dict, list)):
        return _show(v)
    print(quantity(v, r.get("unit", ""), _st()))


def cmd_devices(a):
    from .term import table
    rows = _client(a).devices(" ".join(a.select) or None, a.by or a.sort)
    if a.json:
        return _show(rows)
    st = _st()
    if not rows:
        return print("no devices match" if a.select else "no devices")

    def row(r):
        if r.get("online") is False:
            state = st.bad("offline")
        elif r.get("duplicate_of"):
            state = st.warn(f"same instrument as {r['duplicate_of']}")
        elif r.get("locked_by"):
            state = st.warn(f"locked by {r['locked_by']}")
        else:
            state = st.good("ready")
        return [st.bold(r["device"]), r.get("kind", ""), r.get("manufacturer", ""), r.get("model", ""),
                st.dim(r.get("serial", "")), state, st.dim(r.get("location") or r.get("description", ""))]
    head = ["DEVICE", "KIND", "MANUFACTURER", "MODEL", "SERIAL", "STATE", "WHERE / WHAT"]
    from .term import width
    cells = [row(r) for r in rows]
    keep = [i for i in range(len(head)) if i in (0, 1, 5) or any(width(c[i]) for c in cells)]   # no empty columns
    head = [head[i] for i in keep]
    row_all = row

    def row(r):                                                     # noqa: F811
        c = row_all(r)
        return [c[i] for i in keep]
    if not a.by:
        return print(table(head, [row(r) for r in rows], st, indent=0))
    groups: dict[str, list] = {}
    for r in rows:
        groups.setdefault(str(r.get(a.by) or "unknown"), []).append(r)
    for i, (g, rs) in enumerate(groups.items()):
        print(("\n" if i else "") + st.accent(g) + st.dim(f"  {len(rs)}"))
        print(table(head, [row(r) for r in rs], st))


def _readings(client, device: str, d: dict, only: list[str] | None = None) -> dict:
    """Every readable attribute (or the named ones) on every channel, in one batch: {(name, channel): reply}."""
    asks = []
    for name, spec in (d.get("attributes") or {}).items():
        if only and name not in only:
            continue
        for ch in spec.get("channels") or [None]:
            asks.append((name, ch))
    if not asks:
        return {}
    replies = client.send([{"op": "get", "device": device, "name": n, **({"channel": c} if c else {})} for n, c in asks])
    return dict(zip(asks, replies, strict=True)) if isinstance(replies, list) else {}


def cmd_describe(a):
    from .term import quantity, span, table
    client, st = _client(a), _st()
    d = client.describe(a.device)
    if a.json:
        return _show(d)
    idn = d.get("identity") or {}
    made = " ".join(x for x in (idn.get("manufacturer"), idn.get("model")) if x)
    print(f"{st.bold(a.device)}  {d.get('kind', '')}  {made}  {st.dim('#' + idn['serial']) if idn.get('serial') else ''}"
          f"  {st.dim('firmware ' + idn['firmware']) if idn.get('firmware') else ''}".rstrip().replace("    ", "  "))
    if d.get("description"):
        print(f"  {d['description']}")
    now = _readings(client, a.device, d)
    rows = []
    for name, spec in (d.get("attributes") or {}).items():
        for ch in spec.get("channels") or [None]:
            r = now.get((name, ch), {})
            val = quantity(r["value"], r.get("unit", ""), st) if r.get("ok") else st.dim(r.get("error", {}).get("code", ""))
            rows.append([st.bold(name), ch or "", val, span(spec), "read/write" if spec.get("access") == "rw" else st.dim("read")])
    if rows:
        print("\n" + table(["ATTRIBUTE", "CH", "NOW", "ALLOWED", "ACCESS"], rows, st))
    fns = d.get("functions") or {}
    if fns:
        print("\n" + table(["FUNCTION", "ARGUMENTS", ""], [[st.bold(n), ", ".join(f"{k}: {t}" for k, t in (f.get("args") or {}).items()),
                                                         st.dim(f.get("doc", ""))] for n, f in fns.items()], st))
    if d.get("streams"):
        print("\n" + table(["STREAM", "CONTENT", ""], [[st.bold(n), x["content_type"].split(";")[0], st.dim(x.get("doc", ""))]
                                                    for n, x in d["streams"].items()], st))
    if d.get("safe"):
        print("\n  " + st.dim("safe state  ") + ", ".join(f"{k} {quantity(v, '', st)}" for k, v in d["safe"].items()))


def cmd_watch(a):
    """Readings refreshed in place until Ctrl-C."""
    import time

    from .term import quantity, table
    client, st = _client(a), _st()
    d = client.describe(a.device)
    try:
        while True:
            now = _readings(client, a.device, d, a.names or None)
            rows = [[st.bold(n), c or "", quantity(r["value"], r.get("unit", ""), st) if r.get("ok") else st.bad(r["error"]["code"])]
                    for (n, c), r in now.items()]
            text = f"{st.bold(a.device)}  {st.dim(time.strftime('%H:%M:%S'))}  {st.dim('Ctrl-C to stop')}\n" + table(["ATTRIBUTE", "CH", "NOW"], rows, st)
            if sys.stdout.isatty():
                print("\x1b[H\x1b[J" + text, flush=True)
            else:
                print(text, flush=True)
            time.sleep(a.interval)
    except KeyboardInterrupt:
        print()


def cmd_stream(a):
    """A device's stream to stdout: lines as they come, or bytes to a file or pipe."""
    import urllib.request
    from urllib.parse import quote
    client = _client(a)
    q = f"?channel={quote(a.channel)}" if a.channel else ""
    req = urllib.request.Request(f"{client.url}/api/v1/streams/{quote(a.device)}/{quote(a.name)}{q}",
                                 headers={"Authorization": f"Bearer {client.token}"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            kind = r.headers.get("Content-Type", "")
            text = kind.startswith(("application/x-ndjson", "text/"))
            if not text and sys.stdout.isatty():
                raise LabratError("bad_request", f"{a.name} sends {kind.split(';')[0]}; send it to a file: labrat stream ... > out.mjpeg")
            out = sys.stdout if text else sys.stdout.buffer
            while chunk := r.read1(65536):
                out.write(chunk.decode(errors="replace") if text else chunk)
                out.flush()
    except urllib.error.HTTPError as e:
        err = json.loads(e.read()).get("error", {})
        raise LabratError(err.get("code", "device"), err.get("message", f"HTTP {e.code}")) from None
    except KeyboardInterrupt:
        pass


def cmd_stop(a):
    st, r = _st(), _client(a).stop(a.reason)
    if a.json:
        return _show(r)
    if r.get("safe"):
        print(st.good("every device is in its safe state"))
        return 0
    print(st.bad("not every device reached its safe state:"))
    for p in r.get("problems", []):
        print(f"  {p}")
    return 2


def cmd_record(a):
    from .term import table
    client, st = _client(a), _st()
    if a.action == "list" or not a.name:
        r = client.recordings()
        if a.json:
            return _show(r)
        print(st.dim("stores  ") + (", ".join(f"{n} {st.dim(p)}" for n, p in r["stores"].items()) or "none"))
        rows = []
        for x in r["recordings"]:
            run = x.get("run") or {}
            what = ", ".join(x.get("read") or []) or x.get("call") or x.get("stream") or ""
            state = st.good("recording") if run.get("running") else (st.bad("error") if run.get("error") else st.dim("idle"))
            rows.append([st.bold(x["recording"]), x.get("device", ""), what, state, str(run.get("readings", "")), st.dim(run.get("file", ""))])
        return print(table(["RECORDING", "DEVICE", "WHAT", "STATE", "READINGS", "FILE"], rows, st, indent=0) if rows else "no recordings")
    spec = {k: getattr(a, k) for k in ("device", "read", "call", "stream", "every_s", "duration_s", "count", "store", "format", "channel")
            if getattr(a, k, None) is not None} if a.action == "start" else {}
    r = client.record(a.name, a.action, **spec)
    if a.json:
        return _show(r)
    state = st.good("recording") if r.get("running") else st.dim("stopped")
    print(f"{st.bold(r['recording'])}  {state}  {r.get('readings', 0)} readings  {st.dim(r.get('file', ''))}"
          + (f"  {st.bad(r['error'])}" if r.get("error") else ""))


def cmd_experiment(a):
    from .term import table
    client, st = _client(a), _st()
    if a.action == "list":
        rows = client.experiments(a.person)
        if a.json:
            return _show(rows)
        if not rows:
            return print("no experiments yet")
        state = {"open": st.good("open"), "ended": st.dim("ended")}
        return print(table(["EXPERIMENT", "PERSON", "STATE", "STARTED", "BY", "FOLDER", "ABOUT"],
                           [[st.bold(e["experiment"]), e.get("person", ""), state.get(e.get("state"), e.get("state", "")),
                             e.get("started", "")[:16].replace("T", " "), st.dim(e.get("started_by", "")), st.dim(e.get("folder", "")),
                             e.get("description", "")] for e in rows], st, indent=0))
    if a.action == "start":
        if not a.name:
            raise LabratError("bad_request", "labrat experiment start <name> [description]")
        r = client.start_experiment(a.name, " ".join(a.text))
    elif a.action == "stop":
        r = client.stop_experiment()
    elif a.action == "note":
        r = client.note(" ".join(([a.name] if a.name else []) + a.text))
    else:
        r = client.experiment(a.name)
    if a.json:
        return _show(r)
    state = st.good("open") if r.get("state") == "open" else st.dim(r.get("state", ""))
    print(f"{st.bold(r['experiment'])}  {state}  {st.dim(r.get('folder', ''))}")
    if r.get("description"):
        print(f"  {r['description']}")
    for n in r.get("notes") or []:
        print(f"  {st.dim(n['time'][11:16])} {n['text']}")
    for x in r.get("recordings") or []:
        print(f"  {st.accent(x['recording'])}  {x.get('readings', 0)} readings  {st.dim(Path(x.get('file', '')).name)}")


def cmd_files(a):
    from .term import table
    client, st = _client(a), _st()
    if a.get:
        print(client.download(a.path, a.output, a.person))
        return
    r = client.request("files", name=a.path or None, args={"person": a.person} if a.person else None)
    if "files" not in r:                                           # a file: print it, as text
        if a.json:
            return _show(r)
        if "base64" in r:
            raise LabratError("bad_request", f"{a.path} is binary; fetch it with: labrat files {a.path} --get")
        sys.stdout.write(r["text"])
        offset = r["offset"] + r["length"]
        while r.get("more"):
            r = client.request("files", name=a.path, args={"offset": offset, **({"person": a.person} if a.person else {})})
            sys.stdout.write(r.get("text", ""))
            offset += r["length"]
        return
    if a.json:
        return _show(r["files"])

    def size(n):
        for unit in ("B", "KB", "MB", "GB"):
            if n < 1024 or unit == "GB":
                return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
            n /= 1024
    rows = [[st.accent(f["name"] + "/") if f["folder"] else f["name"], "" if f["folder"] else size(f["size"]),
             st.dim(f["modified"][:16].replace("T", " "))] for f in r["files"]]
    print(table(["NAME", "SIZE", "MODIFIED"], rows, st, indent=0) if rows else "empty")


def _ago(stamp: str) -> str:
    import time
    try:
        t = time.mktime(time.strptime(stamp[:19], "%Y-%m-%dT%H:%M:%S"))
    except ValueError:
        return stamp
    s = max(0, time.time() - t)
    return "just now" if s < 60 else f"{s / 60:.0f} min ago" if s < 3600 else f"{s / 3600:.0f} h ago" if s < 86400 else stamp[:10]


def cmd_who(a):
    from .term import table
    st, r = _st(), _client(a).who()
    if a.json:
        return _show(r)
    rows = []
    for d in r["devices"]:
        held = st.warn(f"{d['locked_by']} ({d['minutes_left']:g} min left)") if d.get("locked_by") else st.dim("free")
        c = d.get("last_change") or {}
        what = " ".join(str(c[k]) for k in ("name", "channel", "value") if c.get(k) is not None)
        rows.append([st.bold(d["device"]), held, c.get("who", ""), what, st.dim(_ago(c["time"])) if c else ""])
    print(table(["DEVICE", "HELD BY", "LAST CHANGED BY", "CHANGE", "WHEN"], rows, st, indent=0) if rows
          else "nobody holds or has changed a device yet")
    if r.get("require_lock"):
        print(st.dim(f"\nchanges need a lock first on: {', '.join(r['require_lock'])}"))
    if r["people"]:
        print("\n" + table(["PERSON", "LAST ACTIVE"], [[p["name"], st.dim(_ago(p["last_active"]))] for p in r["people"]], st, indent=0))


def cmd_users(a):
    from .term import table
    st, rows = _st(), _client(a).users()
    if a.json:
        return _show(rows)
    print(table(["USER", "ROLE", "FOR", "ADDED", "LAST ACTIVE"],
                [[st.bold(u["name"]) + (st.warn(" god mode") if u.get("god_mode") else ""), u["role"], u.get("for", ""),
                  st.dim(u.get("added_by", u["from"]) if u["from"] == "nest.toml" else f"by {u['added_by']} {u['added'][:10]}"),
                  st.dim(_ago(u["last_active"])) if u.get("last_active") else st.dim("not yet")] for u in rows], st, indent=0))


def cmd_user(a):
    client, st = _client(a), _st()
    if a.action == "add":
        r = client.add_user(a.name, a.role, a.for_)
        print(f"added {st.bold(r['name'])} ({r['role']}{', for ' + r['for'] if r.get('for') else ''}). Their token, shown once:\n\n"
              f"  {r['token']}\n\nexport LABRAT_URL={client.url}\nexport LABRAT_TOKEN={r['token']}")
    else:
        client.remove_user(a.name)
        print(f"removed {st.bold(a.name)}: their token no longer works, their locks are released")


def cmd_approvals(a):
    from .term import table
    st, rows = _st(), _client(a).approvals()
    if not rows:
        return print("nothing waits for approval")
    print(table(["ID", "WHO", "REQUEST", "ASKED"], [[st.bold(p["id"]), p["by"],
               " ".join(str(p["request"].get(k)) for k in ("op", "device", "name", "channel", "value") if p["request"].get(k) is not None),
               st.dim(p["asked"])] for p in rows], st, indent=0))


def cmd_shell(a):
    from .term import shell
    base = [x for k, v in (("--url", a.url), ("--token", a.token)) if v for x in (k, v)]
    return shell(_client(a), lambda args: main(args + base))


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
    p = argparse.ArgumentParser(prog="labrat", description="A universal interface for lab automation.")
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
    s = sub.add_parser("demo", help="a demo lab with simulated instruments")
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
    js = (("--json",), {"action": "store_true", "help": "print JSON"})
    client_cmd("devices", "list the devices, filtered by a selector such as: oscilloscope tektronix/*", cmd_devices,
               (("select",), {"nargs": "*", "help": "kind, manufacturer/model, #serial, @firmware, field=pattern"}),
               (("--by",), {"choices": ["kind", "manufacturer", "model", "location", "rat"], "help": "group by"}),
               (("--sort",), {"choices": ["device", "kind", "manufacturer", "model", "serial", "location", "rat"]}), js)
    client_cmd("kinds", "the standard device kinds", lambda a: [print(f"{k:<24} {v[0]}" + (f"  ({v[1]})" if v[1] else ""))
                                                               for k, v in __import__("labrat.ident", fromlist=["KINDS"]).KINDS.items()])
    client_cmd("describe", "what a device offers, with its current readings", cmd_describe, "device", js)
    client_cmd("get", "read an attribute", lambda a: _print_value(_reply(a, {"op": "get", "device": a.device, "name": a.name, "channel": a.channel}), a.json),
               "device", "name", ch, js)
    client_cmd("set", "set an attribute", lambda a: _print_value(_reply(a, {"op": "set", "device": a.device, "name": a.name, "channel": a.channel,
                                                                              "value": _value(a.value)}), a.json),
               "device", "name", "value", ch, js)
    client_cmd("watch", "live readings of a device, refreshed in place", cmd_watch, "device", (("names",), {"nargs": "*"}),
               (("--interval", "-n"), {"type": float, "default": 1.0}))
    client_cmd("stream", "a device's stream to the terminal, a file or a pipe", cmd_stream, "device", "name", ch)
    client_cmd("shell", "an interactive prompt with tab completion", cmd_shell)
    client_cmd("call", "run a function, arguments as key=value",
               lambda a: _show(_client(a).call(a.device, a.name, a.channel, **{k: _value(v) for k, v in (x.split("=", 1) for x in a.args)})),
               "device", "name", (("args",), {"nargs": "*"}), ch)
    client_cmd("lock", "take a device", lambda a: _show(_client(a).lock(a.device, a.minutes)), "device",
               (("--minutes",), {"type": float}))
    client_cmd("unlock", "release a device", lambda a: _show(_client(a).unlock(a.device)), "device")
    client_cmd("stop", "put every device in its safe state", cmd_stop,
               (("--reason",), {"default": "labrat stop"}), js)
    client_cmd("rats", "rats adopted and seen", lambda a: _show(_client(a).rats()))
    client_cmd("approvals", "requests waiting for a person", cmd_approvals)
    client_cmd("record", "recordings: list, or start, stop or status of one", cmd_record,
               (("action",), {"nargs": "?", "choices": ["list", "start", "stop", "status"], "default": "list"}),
               (("name",), {"nargs": "?"}), (("--device",), {}), (("--read",), {"nargs": "+"}), (("--call",), {}),
               (("--stream",), {}), (("--every",), {"type": float, "dest": "every_s"}), (("--for",), {"type": float, "dest": "duration_s"}),
               (("--count",), {"type": int}), (("--store",), {}), (("--format",), {"choices": ["csv", "jsonl"]}), ch, js)
    client_cmd("experiment", "experiments: list yours, or start, stop, note or status", cmd_experiment,
               (("action",), {"nargs": "?", "choices": ["list", "start", "stop", "note", "status"], "default": "status"}),
               (("name",), {"nargs": "?"}), (("text",), {"nargs": "*", "help": "a description (start) or a note"}),
               (("--person",), {"help": "another person's (admins), or * for everyone's"}), js)
    client_cmd("experiments", "your experiments, newest first", lambda a: cmd_experiment(argparse.Namespace(**vars(a), action="list", name=None, text=[])),
               (("--person",), {}), js)
    client_cmd("files", "the files of your experiments: list a folder, print a file, or --get to download it", cmd_files,
               (("path",), {"nargs": "?", "default": ""}), (("--get",), {"action": "store_true"}), (("--output", "-o"), {}),
               (("--person",), {}), js)
    client_cmd("who", "who holds each device, who changed it last, who was active", cmd_who, js)
    client_cmd("users", "the users of this nest", cmd_users, js)
    client_cmd("user", "add or remove a user (admins), with no restart", cmd_user,
               (("action",), {"choices": ["add", "remove"]}), "name",
               (("--role",), {"choices": ROLES, "default": "operator"}),
               (("--for",), {"dest": "for_", "help": "the person this user (an AI agent, say) works for"}))
    client_cmd("approve", "approve a waiting request (it then runs)", lambda a: _show(_client(a).approve(a.id)), "id")
    client_cmd("deny", "deny a waiting request", lambda a: _show(_client(a).deny(a.id)), "id")
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
        from .term import Style, colour_wanted
        st = Style(colour_wanted(sys.stderr))
        if e.code == "pending":
            print(f"{st.warn('waiting for approval')} {e.message}. Approve with: labrat approve {e.extra.get('approval', '')}", file=sys.stderr)
            return 3
        print(f"{st.bad(e.code)}{st.dim(':')} {e.message}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
