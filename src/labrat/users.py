"""People added and removed while the nest runs, without editing nest.toml or restarting.

    labrat user add ada --role operator            # prints ada's token once
    labrat user add claude-ada --role operator --for ada
    labrat users                                   # everyone, with their role and when they were last active
    labrat user remove ada                         # her token stops working at once

The users live in <state_dir>/users.json, readable by the nest's owner only, with the SHA-256 of each token and
never the token itself. Tokens written in nest.toml keep working beside them and are changed in the file.
Adding and removing needs an admin. God mode is never granted this way: it is set in nest.toml only.
"""
from __future__ import annotations

import json
import os
import secrets
import threading
import time
from pathlib import Path

from .config import check_name
from .message import ROLES, LabratError, Message


class Users:
    def __init__(self, hub, path: Path):
        self.hub, self.path = hub, path
        self.configured = {p.name for p in hub.tokens.values()}       # from nest.toml, changed there only
        self.entries: dict[str, dict] = {}
        self._lock = threading.Lock()
        if path.is_file():
            self.entries = json.loads(path.read_text())
            from .hub import Principal
            for name, e in self.entries.items():
                hub.tokens[e["sha256"]] = Principal(name, e["role"], e.get("for"))

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(self.entries, f, indent=2)
        os.replace(tmp, self.path)

    def listing(self) -> list[dict]:
        seen = self.hub.active_at
        out = []
        for p in sorted({p.name: p for p in self.hub.tokens.values()}.values(), key=lambda p: p.name):
            if p.name == "nest" and getattr(self.hub, "role", "") == "rat":
                continue
            e = self.entries.get(p.name, {})
            out.append({"name": p.name, "role": p.role, **({"for": p.for_} if p.for_ else {}), **({"god_mode": True} if p.god else {}),
                        "from": "nest.toml" if p.name in self.configured else "labrat user add",
                        **({"added": e["added"], "added_by": e["added_by"]} if e else {}),
                        **({"last_active": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(seen[p.name]))} if p.name in seen else {})})
        return out

    def add(self, name: str, role: str, for_: str | None, who) -> dict:
        check_name(name, "user")
        if role not in ROLES:
            raise LabratError("bad_request", f"role is one of {', '.join(ROLES)}")
        if for_:
            check_name(for_, "person")
        with self._lock:
            if name in self.configured or name in self.entries or name == "nest":
                raise LabratError("bad_request", f"there is already a user {name!r}")
            token = secrets.token_urlsafe(24)
            from .hub import Principal, token_hash
            entry = {"role": role, "sha256": token_hash(token), "added": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "added_by": who.name}
            if for_ and for_ != name:
                entry["for"] = for_
            self.entries[name] = entry
            self._save()
            self.hub.tokens[entry["sha256"]] = Principal(name, role, entry.get("for"))
        return {"name": name, "role": role, **({"for": entry["for"]} if "for" in entry else {}), "token": token}

    def remove(self, name: str) -> dict:
        with self._lock:
            if name in self.configured:
                raise LabratError("bad_request", f"{name} is written in nest.toml; remove the [[tokens]] entry there")
            if name not in self.entries:
                raise LabratError("not_found", f"no user {name!r}")
            entry = self.entries.pop(name)
            self._save()
            self.hub.tokens.pop(entry["sha256"], None)
        self.hub.forget_person(name)
        return {"name": name, "removed": True}

    def op(self, m: Message, who) -> dict | list:
        action = m.args.get("action", "list")
        if action == "list":
            return self.listing()
        if who.role != "admin":
            raise LabratError("forbidden", f"{who.name} ({who.role}) may not add or remove users; an admin does that")
        if not m.name:
            raise LabratError("bad_request", f"{action} needs the user's name")
        if action == "add":
            return self.add(m.name, str(m.args.get("role", "operator")), m.args.get("for"), who)
        if action == "remove":
            return self.remove(m.name)
        raise LabratError("bad_request", "args.action is list, add or remove")
