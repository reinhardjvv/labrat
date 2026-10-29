"""Finding nests and rats on the local network with multicast DNS (DNS-SD service _labrat._tcp).

Every nest and rat announces itself with its role, name, version and, for a rat, the fingerprint of its key.
The fingerprint lets a person check that the rat they adopt is the one in front of them. Discovery only finds
things: adopting a rat still needs its key.
"""
from __future__ import annotations

import logging
import socket
import threading
import time
from dataclasses import dataclass

from . import __version__

SERVICE = "_labrat._tcp.local."
log = logging.getLogger("labrat")


@dataclass
class Found:
    name: str
    role: str
    url: str
    version: str = ""
    fingerprint: str = ""
    seen: float = 0.0

    def to_dict(self) -> dict:
        return {"name": self.name, "role": self.role, "url": self.url, "version": self.version,
                "fingerprint": self.fingerprint}


def _zeroconf():
    try:
        import zeroconf
    except ImportError:                                   # a dependency, but keep running without it
        log.warning("discovery is off: the zeroconf package is missing")
        return None
    return zeroconf


def local_addresses() -> list[str]:
    """IPv4 addresses of this computer, loopback left out."""
    try:
        import ifaddr
        addrs = [ip.ip for a in ifaddr.get_adapters() for ip in a.ips if isinstance(ip.ip, str)]
    except ImportError:
        addrs = [socket.gethostbyname(socket.gethostname())]
    return [a for a in addrs if not a.startswith("127.")] or ["127.0.0.1"]


class Advertiser:
    """Announce this nest or rat until close() is called."""

    def __init__(self, role: str, name: str, port: int, fingerprint: str = ""):
        self.zc = self.info = None
        zc = _zeroconf()
        if zc is None:
            return
        props = {"role": role, "name": name, "version": __version__}
        if fingerprint:
            props["fp"] = fingerprint
        self.info = zc.ServiceInfo(SERVICE, f"{name}.{SERVICE}", port=port, properties=props,
                                   parsed_addresses=local_addresses(), server=f"{socket.gethostname().split('.')[0]}.local.")
        try:
            self.zc = zc.Zeroconf()
            self.zc.register_service(self.info, allow_name_change=True)
            log.info("announcing %s %s on port %d", role, name, port)
        except Exception as e:
            log.warning("cannot announce on the network (%s); discovery will not find this %s", e, role)
            self.close()

    def close(self) -> None:
        if self.zc is not None:
            try:
                if self.info is not None:
                    self.zc.unregister_service(self.info)
                self.zc.close()
            except Exception:
                pass
            self.zc = None


class Browser:
    """Keep a live list of the nests and rats on the network."""

    def __init__(self):
        self.found: dict[str, Found] = {}
        self._lock = threading.Lock()
        self.zc = None
        zc = _zeroconf()
        if zc is None:
            return
        try:
            self.zc = zc.Zeroconf()
            self.browser = zc.ServiceBrowser(self.zc, SERVICE, handlers=[self._changed])
        except Exception as e:
            log.warning("cannot browse the network (%s); discovery is off", e)
            self.zc = None

    def _changed(self, zeroconf, service_type, name, state_change) -> None:
        if state_change.name == "Removed":
            with self._lock:
                self.found = {k: v for k, v in self.found.items() if k != name}
            return
        info = zeroconf.get_service_info(service_type, name, timeout=2000)
        if info is None or not info.parsed_addresses():
            return
        p = {k.decode(): (v or b"").decode() for k, v in info.properties.items()}
        addr = info.parsed_addresses()[0]
        host = f"[{addr}]" if ":" in addr else addr
        with self._lock:
            self.found[name] = Found(p.get("name", name.split(".")[0]), p.get("role", "?"), f"http://{host}:{info.port}",
                                     p.get("version", ""), p.get("fp", ""), time.time())

    def snapshot(self, role: str | None = None) -> list[Found]:
        with self._lock:
            return [f for f in self.found.values() if role is None or f.role == role]

    def close(self) -> None:
        if self.zc is not None:
            self.zc.close()
            self.zc = None


def scan(seconds: float = 3.0) -> list[Found]:
    """Everything that answers within the given time."""
    b = Browser()
    try:
        time.sleep(seconds)
        return sorted(b.snapshot(), key=lambda f: (f.role, f.name))
    finally:
        b.close()
