"""The workbench: from an unknown box on a port to a normal LABRAT device.

    scan       what is plugged in, and which catalog profiles fit it
    probe      safe questions to a device, kept as a transcript
    capture    what the vendor's software says to it (serial pass-through or usbmon)
    profile    the catalog: list and show profiles, try a draft against the device, save it
    device     add a device from a profile, or remove it, while the nest or rat runs

Every operation runs on the computer the device is plugged into: the nest passes it to a rat named in
args.rat. Scans and listings are for everyone. Probing, captures, trying and saving drafts need an operator.
Adding and removing devices and marking a profile tested need an admin.
"""
from __future__ import annotations

import json
import logging
import threading
from pathlib import Path

from .message import ROLES, LabratError, Message

log = logging.getLogger("labrat")
OPS = ("scan", "probe", "capture", "profile", "device")


def required_role(m: Message) -> str:
    reading = m.args.get("action", "list") in ("list", "show")
    return {"scan": "operator" if m.args.get("probe") else "viewer", "probe": "operator", "capture": "operator",
            "profile": "viewer" if reading else "operator", "device": "admin"}[m.op]


def check_role(m: Message, who) -> None:
    need = required_role(m)
    if ROLES.index(who.role) < ROLES.index(need):
        raise LabratError("forbidden", f"{who.name} ({who.role}) may not {m.op} {m.args.get('action', '')}".strip()
                          + f"; that needs {'an' if need[0] in 'ao' else 'a'} {need}")


class Workbench:
    def __init__(self, hub, state_dir: Path):
        self.hub, self.state = hub, Path(state_dir)
        self.captures: dict[str, object] = {}
        self.added: dict[str, dict] = {}                               # devices added while running: name -> config
        self._lock = threading.Lock()
        self.devices_file = self.state / "devices.json"
        if self.devices_file.is_file():
            from .drivers import build
            for name, cfg in json.loads(self.devices_file.read_text()).items():
                try:
                    hub.devices[name] = build(name, cfg)
                    self.added[name] = cfg
                except LabratError as e:
                    log.error("device %s, added earlier, did not start: %s", name, e.message)
        self._offer()

    def _offer(self) -> None:
        """Say in the log which plugged-in devices a profile fits, so nobody has to go looking."""
        try:
            from .detect import scan
            for row in scan(self.hub):
                if row["profiles"] and not row["in_use"]:
                    p = row["profiles"][0]
                    log.info("%s on %s fits profile %s: add it with 'labrat add <name> --profile %s --port %s'",
                             row.get("product") or row["usb"], ", ".join(row["files"]) or "USB", p["profile"], p["profile"],
                             (row["files"] or ["<port>"])[0])
        except Exception:                                              # never stop a start for a hint
            log.debug("scan at start failed", exc_info=True)

    # -- dispatch -------------------------------------------------------------------------------------------
    def op(self, m: Message, who):
        if who is not getattr(self.hub, "nest", None):                  # a rat trusts what its nest already checked
            check_role(m, who)
        return getattr(self, m.op)(m, who)

    def _busy(self, target: str) -> None:
        bare = target.split("://")[-1]
        for d in self.hub.devices.values():
            for k in ("port", "address"):
                v = str(d.config.get(k, ""))
                if v and v.split("://")[-1].split("?")[0] == bare:
                    raise LabratError("locked", f"{target} serves the device {d.name}; remove it or use another port")

    # -- scan and probe -------------------------------------------------------------------------------------
    def scan(self, m: Message, who):
        from .detect import scan
        return scan(self.hub, bool(m.args.get("probe")))

    def probe(self, m: Message, who):
        from .probe import Probe, save
        target = str(m.args.get("target") or "")
        if not target:
            raise LabratError("bad_request", "probe needs args.target: a port, hid://, tcp://, http:// or i2c:<bus>")
        self._busy(target)
        p = Probe(target, str(m.args.get("protocol", "auto")), m.args.get("sends"), m.args.get("baud"),
                  m.args.get("line_ending"), approved=m.approved or (who is getattr(self.hub, "nest", None) and m.args.get("approved") is True))
        if p.needs_approval():
            raise self.hub.ask_approval(m, who)                          # even in god mode: a wrong guess can damage hardware
        t = p.run()
        save(t, self.state / "probes")
        return t

    # -- captures -------------------------------------------------------------------------------------------
    def capture(self, m: Message, who):
        from .capture import Capture
        action = m.args.get("action", "status")
        if action == "start":
            kind, target = str(m.args.get("kind", "serial")), str(m.args.get("target", ""))
            if kind == "serial":
                self._busy(target)
            with self._lock:
                if target in self.captures and self.captures[target].thread.is_alive():
                    raise LabratError("locked", f"a capture of {target} is already running")
                c = Capture(kind, target, self.state / "captures", int(m.args.get("baud", 9600)), m.args.get("link"))
                self.captures[target] = c
            return c.status()
        if action == "stop":
            target = m.args.get("target") or next((t for t, c in self.captures.items() if c.thread.is_alive()), None)
            if target not in self.captures:
                raise LabratError("not_found", "no capture is running")
            return self.captures.pop(target).stop()
        return [c.status() for c in self.captures.values()]

    def _transcripts(self, names: list[str]) -> list[dict]:
        out = []
        for n in names or []:
            for folder in ("probes", "captures"):
                p = (self.state / folder / str(n)).resolve()
                if p.is_relative_to((self.state / folder).resolve()) and p.is_file():
                    out.append(json.loads(p.read_text()))
                    break
            else:
                raise LabratError("not_found", f"no probe or capture {n!r}")
        return out

    # -- profiles -------------------------------------------------------------------------------------------
    def profile(self, m: Message, who):
        from . import catalog
        cat = catalog.default()
        action = m.args.get("action", "list")
        if action == "list":
            return cat.listing(m.args.get("select"))
        if action == "show":
            p = cat.get(str(m.name or m.args.get("profile")))
            return {**p.summary(), "matches": p.matches, "toml": p.path.read_text(encoding="utf-8") if p.path else catalog.dump(p.data)}
        text = m.args.get("toml")
        prof = catalog.parse(str(text)) if text else cat.get(str(m.name or m.args.get("profile")))
        if action == "try":
            return self._try(prof, dict(m.args.get("device") or {}))
        if action == "save":
            if not text:
                raise LabratError("bad_request", "save needs the profile as args.toml")
            if prof.meta["status"] == "tested" and who.role != "admin":
                raise LabratError("forbidden", "only an admin marks a profile tested; save it as status = \"draft\"")
            have = {(r.get("send"), r.get("reply"), r.get("send_hex"), r.get("reply_hex")) for r in prof.recordings}
            new = [r for r in catalog.recordings_from(self._transcripts(m.args.get("transcripts")) + list(m.args.get("recordings") or []))
                   if (r.get("send"), r.get("reply"), r.get("send_hex"), r.get("reply_hex")) not in have]
            if new:
                text = str(text).rstrip() + "\n\n" + catalog.dump({"recording": new})
                prof = catalog.parse(text)
            path = cat.save(prof, str(text), bool(m.args.get("overwrite")))
            return {**cat.get(prof.id).summary(), "file": str(path), "recordings_added": len(new)}
        raise LabratError("bad_request", "args.action is list, show, try or save")

    def _try(self, prof, settings: dict) -> dict:
        """Build the profile's device on the real port, read everything it can read, and change nothing."""
        from .catalog import merge
        from .drivers import build
        target = str(settings.get("port") or settings.get("address") or settings.get("url") or "")
        if target and not settings.get("simulate"):
            self._busy(target)
        dev = build("try", merge(prof, settings), profile=prof)
        link = getattr(dev, "link", None)
        if link is not None:
            link.transcript = []
        readings, problems = {}, {}
        try:
            for name, a in dev.attrs.items():
                for ch in a.channels or [None]:
                    key = f"{name}[{ch}]" if ch else name
                    try:
                        readings[key] = dev.get(name, ch)
                    except Exception as e:
                        problems[key] = getattr(e, "message", str(e))
        finally:
            dev.close()
        return {"profile": prof.id, "readings": readings, "problems": problems,
                "exchanges": getattr(link, "transcript", None) or [],
                "verdict": "every attribute read" if not problems else f"{len(problems)} of {len(readings) + len(problems)} did not read"}

    # -- devices added while running ------------------------------------------------------------------------
    def device(self, m: Message, who):
        from .drivers import build
        action, name = m.args.get("action", "add"), str(m.name or "")
        from .config import check_name
        check_name(name, "device")
        with self._lock:
            if action == "add":
                if name in self.hub.devices:
                    raise LabratError("bad_request", f"there is already a device {name!r}")
                cfg = dict(m.args.get("config") or {})
                if not cfg.get("profile") and not cfg.get("driver"):
                    raise LabratError("bad_request", "args.config needs a profile or a driver")
                for k in ("port", "address"):
                    if cfg.get(k) and not cfg.get("simulate"):
                        self._busy(str(cfg[k]))
                dev = build(name, cfg)
                for attr, lim in (getattr(self.hub, "limits", {}).get(name) or {}).items():
                    dev.limit(attr, lim)
                self.hub.devices[name] = dev
                self.added[name] = cfg
            elif action == "remove":
                if name not in self.added:
                    raise LabratError("bad_request" if name in self.hub.devices else "not_found",
                                      f"{name} is in the configuration file; remove it there" if name in self.hub.devices
                                      else f"no device {name!r}")
                self.added.pop(name)
                dev = self.hub.devices.pop(name)
                problems = dev.make_safe()
                dev.close()
            else:
                raise LabratError("bad_request", "args.action is add or remove")
            tmp = self.devices_file.with_suffix(".tmp")
            self.devices_file.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(self.added, indent=2))
            tmp.replace(self.devices_file)
        if action == "add":
            log.info("device %s added by %s%s", name, who.name, f" from profile {dev.profile}" if dev.profile else "")
            return {"device": name, "added": True, **dev.identity(), **({"profile": dev.profile} if dev.profile else {})}
        return {"device": name, "removed": True, **({"problems": problems} if problems else {})}

    def close(self) -> None:
        for c in list(self.captures.values()):
            if c.thread.is_alive():
                c.stop()
