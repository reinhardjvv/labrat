"""Device profiles: how a model talks, how to recognise it, and the real replies it was worked out from.

A profile is a TOML file at <catalog>/<manufacturer>/<model>.toml:

    [profile]
    manufacturer = "mecer"
    model = "ME-2000-WPRU"
    kind = "ups"
    description = "2000 VA on-line UPS speaking the Voltronic variant of the Megatec protocol"
    evidence = "replies recorded from a unit with firmware 01833.01 on 20 Oct 2026"
    status = "tested"                     # tested, or draft for a profile nobody has checked yet

    [[match]]                             # any one of these recognises the model
    usb = "0665:5161"                     # USB vendor:product
    probe = "QMD"                         # and, to be sure, a read-only query and what its reply looks like
    reply = 'WPHVR2K0'

    [device]                              # what a [devices.<name>] table would hold, without the port
    driver = "serial"
    line_ending = "cr"
    [device.attributes.BatteryCharge]
    get = "QBV"
    reply = '^\\((?:\\S+ ){3}(\\S+)'
    unit = "%"

    [[recording]]                         # real exchanges: the evidence, the simulator and the tests
    send = "QBV"
    reply = "(054.6 04 01 094 306"

A device uses a profile with profile = "<manufacturer>/<model>" and its own port; its table overrides the
profile's. With simulate = true it answers from the recordings, which suits tests, demos and trying a lab
layout before the hardware arrives. Profiles come with LABRAT and from the folders in catalog (by default
<state_dir>/profiles); a folder listed first wins.
"""
from __future__ import annotations

import re
import time
import tomllib
from fnmatch import fnmatch
from pathlib import Path

from .message import LabratError

BUILTIN = Path(__file__).parent / "profiles"
MATCH_KEYS = {"usb", "idn", "probe", "reply"}
PROFILE_KEYS = {"id", "manufacturer", "model", "kind", "description", "evidence", "status", "url"}


class Profile:
    def __init__(self, data: dict, path: Path | None = None, builtin: bool = False):
        unknown = set(data) - {"profile", "match", "device", "recording"}
        where = path.name if path else "profile"
        if unknown:
            raise LabratError("bad_request", f"{where}: unknown table(s) {', '.join(sorted(unknown))}")
        self.meta = dict(data.get("profile") or {})
        if set(self.meta) - PROFILE_KEYS:
            raise LabratError("bad_request", f"{where}: [profile] takes {', '.join(sorted(PROFILE_KEYS))}")
        for k in ("manufacturer", "model"):
            if not self.meta.get(k):
                raise LabratError("bad_request", f"{where}: [profile] needs {k}")
        from .ident import maker, slug
        self.meta["manufacturer"] = maker(str(self.meta["manufacturer"]))
        self.id = str(self.meta.get("id") or f"{self.meta['manufacturer']}/{slug(str(self.meta['model']))}")
        self.meta.setdefault("status", "draft")
        if self.meta["status"] not in ("tested", "draft"):
            raise LabratError("bad_request", f"{self.id}: status is tested or draft")
        self.matches = [dict(m) for m in data.get("match") or []]
        for m in self.matches:
            if not m or set(m) - MATCH_KEYS:
                raise LabratError("bad_request", f"{self.id}: a [[match]] takes {', '.join(sorted(MATCH_KEYS))}")
            if "usb" in m and not re.fullmatch(r"[0-9a-fA-F]{4}:[0-9a-fA-F]{4}", str(m["usb"])):
                raise LabratError("bad_request", f"{self.id}: usb is vendor:product in hex, such as 0665:5161")
            if ("probe" in m) != ("reply" in m):
                raise LabratError("bad_request", f"{self.id}: a probe needs the reply it should give, and the other way round")
        self.device = dict(data.get("device") or {})
        if not self.device.get("driver"):
            raise LabratError("bad_request", f"{self.id}: [device] needs a driver")
        self.recordings = [dict(r) for r in data.get("recording") or []]
        for r in self.recordings:
            if not (("send" in r and "reply" in r) or ("send_hex" in r and "reply_hex" in r)):
                raise LabratError("bad_request", f"{self.id}: a [[recording]] is send and reply, or send_hex and reply_hex")
        self.path, self.builtin, self.data = path, builtin, data

    def summary(self) -> dict:
        return {"profile": self.id, "manufacturer": self.meta["manufacturer"], "model": self.meta["model"],
                "kind": self.meta.get("kind", ""), "driver": self.device["driver"], "status": self.meta["status"],
                "recordings": len(self.recordings), "description": self.meta.get("description", ""),
                "usb": [m["usb"].lower() for m in self.matches if "usb" in m],
                "source": "built in" if self.builtin else str(self.path) if self.path else "draft"}

    def identity(self) -> dict:
        out = {k: self.meta[k] for k in ("manufacturer", "model") if self.meta.get(k)}
        if self.meta.get("kind"):
            out["kind"] = self.meta["kind"]
        return out


def parse(text: str, path: Path | None = None) -> Profile:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise LabratError("bad_request", f"{path.name if path else 'profile'}: {e}") from None
    return Profile(data, path)


class Catalog:
    def __init__(self, dirs: list[Path] | None = None):
        self.dirs = [Path(d).expanduser() for d in (dirs or [])]
        self.reload()

    def reload(self) -> None:
        self.profiles: dict[str, Profile] = {}
        self.problems: list[str] = []
        for folder, builtin in [(BUILTIN, True)] + [(d, False) for d in reversed(self.dirs)]:   # earlier folders win
            for f in sorted(folder.glob("*/*.toml")) if folder.is_dir() else []:
                try:
                    p = Profile(tomllib.loads(f.read_text(encoding="utf-8")), f, builtin)
                except (LabratError, tomllib.TOMLDecodeError, OSError) as e:
                    self.problems.append(f"{f}: {getattr(e, 'message', e)}")
                    continue
                self.profiles[p.id] = p

    def get(self, pid: str) -> Profile:
        if pid not in self.profiles:
            import difflib
            near = difflib.get_close_matches(pid, list(self.profiles), n=3)
            raise LabratError("not_found", f"no profile {pid!r}" + (f"; did you mean {', '.join(near)}?" if near else
                                                                   " (labrat profiles lists them)"))
        return self.profiles[pid]

    def listing(self, select: str | None = None) -> list[dict]:
        rows = [p.summary() for p in self.profiles.values()]
        if select:
            from .ident import matches, parse_selector
            sel = parse_selector(select)
            word = sel.pop("device", None)                     # a bare word: a profile id or model pattern
            rows = [r for r in rows if matches(r, sel) and (word is None or fnmatch(r["profile"], word)
                                                            or fnmatch(r["model"].lower(), word.lower()))]
        return sorted(rows, key=lambda r: r["profile"])

    def by_usb(self, usb: str) -> list[Profile]:
        return [p for p in self.profiles.values() if any(m.get("usb", "").lower() == usb.lower() for m in p.matches)]

    def by_idn(self, reply: str) -> list[Profile]:
        return [p for p in self.profiles.values() if any(m.get("idn") and fnmatch(reply.strip(), m["idn"]) for m in p.matches)]

    def by_reply(self, send: str, reply: str) -> list[Profile]:
        """Profiles whose probe is this query and whose expected reply this is."""
        out = []
        for p in self.profiles.values():
            for m in p.matches:
                if m.get("probe") == send and re.search(str(m["reply"]), reply):
                    out.append(p)
                    break
        return out

    def save(self, profile: Profile, text: str, overwrite: bool = False) -> Path:
        if not self.dirs:
            raise LabratError("unavailable", "no catalog folder to save into (set catalog in the configuration)")
        target = self.dirs[0] / profile.id.split("/")[0] / (profile.id.split("/")[1] + ".toml")
        if target.exists() and not overwrite:
            raise LabratError("bad_request", f"{target} exists; save it with overwrite to replace it")
        if profile.id in self.profiles and self.profiles[profile.id].builtin and not overwrite:
            raise LabratError("bad_request", f"{profile.id} comes with LABRAT; save it with overwrite to use yours instead")
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(target)
        self.reload()
        return target


_default: Catalog | None = None


def default() -> Catalog:
    global _default
    if _default is None:
        _default = Catalog()
    return _default


def configure(dirs: list[Path]) -> Catalog:
    global _default
    _default = Catalog(dirs)
    for p in _default.problems:
        import logging
        logging.getLogger("labrat").warning("profile skipped: %s", p)
    return _default


# -- profiles as files -----------------------------------------------------------------------------------------
def merge(profile: Profile, config: dict) -> dict:
    """The device table a profile and a lab's own settings make together. The lab's settings win, attribute by
    attribute, and the profile's identity fills what the lab leaves out."""
    out = {**profile.device, **{k: v for k, v in config.items() if k not in ("attributes", "functions", "profile", "simulate")}}
    for table in ("attributes", "functions"):
        mine = dict(profile.device.get(table) or {})
        for name, spec in (config.get(table) or {}).items():
            mine[name] = {**mine.get(name, {}), **spec}
        if mine:
            out[table] = mine
    for k, v in profile.identity().items():
        out.setdefault(k, v)
    if config.get("simulate"):
        key = "port" if profile.device["driver"] in ("serial", "frames") else "address"
        out[key] = f"replay://{profile.id}"
        out.pop("port" if key == "address" else "address", None)
        out["identify"] = False
    return out


def dump(data: dict) -> str:
    """TOML for a profile-shaped dict: tables, arrays of tables, and plain values."""
    lines: list[str] = []

    def val(v) -> str:
        if isinstance(v, bool):
            return "true" if v else "false"
        if isinstance(v, (int, float)):
            return repr(v)
        if isinstance(v, str):
            if "'" not in v and "\n" not in v and ("\\" in v or '"' in v):
                return f"'{v}'"
            return '"' + v.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'
        if isinstance(v, list):
            return "[" + ", ".join(val(x) for x in v) + "]"
        if isinstance(v, dict):
            return "{ " + ", ".join(f"{key(k)} = {val(x)}" for k, x in v.items()) + " }"
        raise TypeError(f"cannot write {type(v).__name__} as TOML")

    def key(k: str) -> str:
        return k if re.fullmatch(r"[A-Za-z0-9_-]+", k) else val(k)

    def table(name: str, d: dict) -> None:
        plain = {k: v for k, v in d.items() if not isinstance(v, dict) and not (isinstance(v, list) and v and isinstance(v[0], dict))}
        lines.append(f"[{name}]")
        lines.extend(f"{key(k)} = {val(v)}" for k, v in plain.items())
        lines.append("")
        for k, v in d.items():
            if isinstance(v, dict):
                if all(isinstance(x, dict) for x in v.values()) and v:
                    for sub, spec in v.items():
                        table(f"{name}.{key(k)}.{key(sub)}", spec)
                else:
                    table(f"{name}.{key(k)}", v)

    for name, part in data.items():
        if isinstance(part, list):
            for item in part:
                lines.append(f"[[{name}]]")
                lines.extend(f"{key(k)} = {val(v)}" for k, v in item.items())
                lines.append("")
        else:
            table(name, part)
    return "\n".join(lines).rstrip() + "\n"


def recordings_from(transcripts: list[dict]) -> list[dict]:
    """The exchanges of probe, try and capture transcripts that got a reply, once each, as [[recording]]."""
    seen, out = set(), []
    for t in transcripts:
        for x in t.get("exchanges", []):
            if x.get("reply") is not None and "send" in x:
                k = ("send", x["send"], x["reply"])
                rec = {"send": x["send"], "reply": x["reply"]}
            elif x.get("reply_hex") is not None and "send_hex" in x:
                k = ("hex", x["send_hex"], x["reply_hex"])
                rec = {"send_hex": x["send_hex"], "reply_hex": x["reply_hex"]}
            else:
                continue
            if k not in seen:
                seen.add(k)
                out.append(rec)
    return out


def stamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")
