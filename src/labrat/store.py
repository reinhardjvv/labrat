"""Stores: the folders recordings and experiments write into, with an optional local buffer.

    [stores.nas]
    path = "/mnt/labnas/labrat"           # a local folder or a mounted network share
    mount = "/mnt/labnas"                  # optional: write only while this is mounted
    buffer = "/var/lib/labrat/buffer/nas"  # optional: write here first, copy to path when it can be reached
    sync_s = 10                            # how often the buffer is copied across

Without a buffer, a store whose mount is missing refuses new files, so data never lands on the empty mount
point by mistake. With a buffer, files are written locally and copied to the store every sync_s seconds. A
file leaves the buffer once its copy matches and nothing is still writing to it. A network share that drops
for an hour then costs nothing: the copies catch up when it comes back.
"""
from __future__ import annotations

import logging
import os
import shutil
import threading
import time
from pathlib import Path

from .config import check_name
from .message import LabratError

log = logging.getLogger("labrat")
KEYS = {"path", "mount", "buffer", "sync_s"}


class Store:
    def __init__(self, name: str, spec: dict):
        check_name(name, "store")
        if not isinstance(spec, dict) or not spec.get("path"):
            raise LabratError("bad_request", f"store {name} needs a path")
        unknown = set(spec) - KEYS
        if unknown:
            raise LabratError("bad_request", f"store {name}: unknown setting(s) {', '.join(sorted(unknown))}")
        self.name = name
        self.path = Path(str(spec["path"])).expanduser()
        self.mount = Path(str(spec["mount"])).expanduser() if spec.get("mount") else None
        self.buffer = Path(str(spec["buffer"])).expanduser() if spec.get("buffer") else None
        self.sync_s = float(spec.get("sync_s", 10))
        if not 0.1 <= self.sync_s <= 3600:
            raise LabratError("bad_request", f"store {name}: sync_s is between 0.1 and 3600")
        self._open: set[Path] = set()                   # files being written, relative to the store
        self._lock = threading.Lock()
        self.last_sync: float | None = None
        self.error: str | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        if self.buffer is not None:
            self._thread = threading.Thread(target=self._sync_loop, name=f"labrat-sync-{name}", daemon=True)
            self._thread.start()

    # -- where to write and read ----------------------------------------------------------------------------
    def available(self) -> bool:
        """True when the store itself can be written: its mount, if named, is mounted."""
        if self.mount is not None and not os.path.ismount(self.mount):
            return False
        return True

    def root(self) -> Path:
        """The folder new files go into: the buffer when there is one, otherwise the store."""
        if self.buffer is not None:
            return self.buffer
        if not self.available():
            raise LabratError("unavailable", f"store {self.name}: {self.mount} is not mounted")
        return self.path

    def opened(self, path: Path) -> None:
        with self._lock:
            self._open.add(self._rel(path))

    def closed(self, path: Path) -> None:
        with self._lock:
            self._open.discard(self._rel(path))

    def _rel(self, path: Path) -> Path:
        path = Path(path)
        for base in (self.buffer, self.path):
            if base is not None and path.is_relative_to(base):
                return path.relative_to(base)
        return path

    def find(self, rel: Path) -> Path | None:
        """The newest copy of a file or folder: in the buffer if it is still there, otherwise in the store."""
        for base in (self.buffer, self.path if self.available() else None):
            if base is not None and (base / rel).exists():
                return base / rel
        return None

    def listing(self, rel: Path) -> list[dict]:
        """A folder's contents, from the buffer and the store together."""
        out: dict[str, dict] = {}
        for base in (self.path if self.available() else None, self.buffer):     # the buffer's copy wins
            folder = base / rel if base is not None else None
            if folder is None or not folder.is_dir():
                continue
            for p in folder.iterdir():
                try:
                    s = p.stat()
                except OSError:
                    continue
                out[p.name] = {"name": p.name, "folder": p.is_dir(), "size": None if p.is_dir() else s.st_size,
                               "modified": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(s.st_mtime))}
        return sorted(out.values(), key=lambda e: (not e["folder"], e["name"]))

    def status(self) -> dict:
        out = {"path": str(self.path), "available": self.available()}
        if self.mount is not None:
            out["mount"] = str(self.mount)
        if self.buffer is not None:
            out.update(buffer=str(self.buffer), waiting=self.waiting())
            if self.last_sync:
                out["last_sync"] = time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(self.last_sync))
        if self.error:
            out["error"] = self.error
        return out

    # -- the buffer -----------------------------------------------------------------------------------------
    def waiting(self) -> int:
        """Files in the buffer not yet in the store."""
        if self.buffer is None or not self.buffer.is_dir():
            return 0
        return sum(1 for p in self.buffer.rglob("*") if p.is_file() and not p.name.endswith(".part"))

    def sync(self) -> int:
        """Copy the buffer to the store once. Returns the files copied. Never raises."""
        if self.buffer is None or not self.buffer.is_dir():
            return 0
        if not self.available():
            self.error = f"{self.mount} is not mounted; {self.waiting()} file(s) wait in {self.buffer}"
            return 0
        copied = 0
        try:
            for src in sorted(self.buffer.rglob("*")):
                if not src.is_file() or src.name.endswith(".part"):
                    continue
                rel = src.relative_to(self.buffer)
                dst = self.path / rel
                with self._lock:
                    still_open = rel in self._open
                s = src.stat()
                if not (dst.is_file() and dst.stat().st_size == s.st_size and dst.stat().st_mtime >= s.st_mtime):
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    tmp = dst.with_name(dst.name + ".part")
                    shutil.copy2(src, tmp)
                    os.replace(tmp, dst)
                    copied += 1
                if not still_open and src.stat().st_mtime == s.st_mtime and dst.stat().st_size == s.st_size:
                    src.unlink()                        # copied, finished and unchanged since: the store has it
            for d in sorted((p for p in self.buffer.rglob("*") if p.is_dir()), key=lambda p: -len(p.parts)):
                try:
                    d.rmdir()                           # empty folders only
                except OSError:
                    pass
            self.error = None
            self.last_sync = time.time()
        except OSError as e:
            self.error = f"copying to {self.path}: {e}"
            log.warning("store %s: %s", self.name, self.error)
        return copied

    def _sync_loop(self) -> None:
        while not self._stop.wait(self.sync_s):
            self.sync()

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self.sync()                                 # a last copy of whatever finished


def stores_from(cfg: dict) -> dict[str, Store]:
    return {n: Store(n, s) for n, s in (cfg.get("stores") or {}).items()}
