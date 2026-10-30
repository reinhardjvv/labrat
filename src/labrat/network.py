"""A private network of the nest's or a rat's own, on a spare Ethernet port or a Wi-Fi card as an access point.

    [network]
    interface = "wlan1"           # the port or card that carries it
    ssid = "labrat-bench"         # Wi-Fi only; without it the interface is Ethernet
    address = "10.42.0.1/24"      # this computer on that network, and its size
    internet = false              # whether the devices on it can reach beyond it
    [network.reserve]             # fixed addresses by MAC, numbered from .10 in this order unless given
    afg = "08:00:11:00:00:0a"
    scope = { mac = "08:00:11:22:33:44", address = "10.42.0.30" }

NetworkManager runs the network ("shared" mode: addresses by DHCP, names by DNS). LABRAT writes its connection
file, the address reservations and, for a network without internet, a firewall rule that stops traffic
crossing the computer. `labrat network plan` shows all of it, `up` applies it with sudo, `down` removes it.
"""
from __future__ import annotations

import ipaddress
import json
import os
import re
import secrets
import shutil
import subprocess
import uuid
from pathlib import Path

from .message import LabratError

KEYS = {"interface", "ssid", "band", "channel", "address", "internet", "reserve"}
NM = Path("/etc/NetworkManager")
MAC = re.compile(r"[0-9a-f]{2}(:[0-9a-f]{2}){5}")
ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"     # no 0/o or 1/l/i, as people type it into devices


class Network:
    def __init__(self, cfg: dict):
        net = cfg.get("network")
        where = "[network]"
        if not isinstance(net, dict):
            raise LabratError("bad_request", f"{where} is a table")
        if set(net) - KEYS:
            raise LabratError("bad_request", f"{where}: unknown setting(s) {', '.join(sorted(set(net) - KEYS))}; "
                                             f"it takes {', '.join(sorted(KEYS))}")
        self.interface = str(net.get("interface") or "")
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,15}", self.interface):
            raise LabratError("bad_request", f"{where}: interface is the name of a network port, such as eth1 or wlan1")
        self.name = f"labrat-{cfg['name']}"
        self.wifi = "ssid" in net
        self.ssid = str(net.get("ssid", ""))
        if self.wifi and not 1 <= len(self.ssid.encode()) <= 32:
            raise LabratError("bad_request", f"{where}: ssid is 1 to 32 bytes")
        for k in ("band", "channel"):
            if k in net and not self.wifi:
                raise LabratError("bad_request", f"{where}: {k} is for Wi-Fi, which needs an ssid")
        self.band = {"2.4": "bg", "5": "a"}.get(str(net.get("band", "2.4")))
        if self.band is None:
            raise LabratError("bad_request", f"{where}: band is \"2.4\" or \"5\" (GHz)")
        self.channel = net.get("channel")
        if self.channel is not None and (isinstance(self.channel, bool) or not isinstance(self.channel, int) or self.channel < 1):
            raise LabratError("bad_request", f"{where}: channel is a Wi-Fi channel number")
        try:
            self.address = ipaddress.ip_interface(str(net.get("address", "10.42.0.1/24")))
        except ValueError:
            raise LabratError("bad_request", f"{where}: address is this computer's address with the network size, "
                                             "such as 10.42.0.1/24") from None
        if self.address.version != 4 or not self.address.network.is_private or self.address.network.prefixlen > 29 \
                or self.address.ip in (self.address.network.network_address, self.address.network.broadcast_address):
            raise LabratError("bad_request", f"{where}: address is a private IPv4 address in a network of at least "
                                             "8 addresses, such as 10.42.0.1/24")
        self.internet = net.get("internet", False)
        if not isinstance(self.internet, bool):
            raise LabratError("bad_request", f"{where}: internet is true or false")
        self.reserve = self._reservations(net.get("reserve") or {}, where)
        self.psk_path = Path(cfg["state_dir"]) / "network-password"
        self.port = cfg.get("port")

    def _reservations(self, table: dict, where: str) -> dict[str, tuple[str, str]]:
        if not isinstance(table, dict):
            raise LabratError("bad_request", f"{where}: reserve is a table of name = \"mac\"")
        out: dict[str, tuple[str, str]] = {}
        taken = {str(self.address.ip)}
        given = []
        for name, v in table.items():
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,62}", name):
                raise LabratError("bad_request", f"{where}: reservation {name!r} needs a host name of letters, digits and '-'")
            spec = v if isinstance(v, dict) else {"mac": v}
            if set(spec) - {"mac", "address"}:
                raise LabratError("bad_request", f"{where}: reservation {name} takes mac and address")
            mac = str(spec.get("mac", "")).lower().replace("-", ":")
            if not MAC.fullmatch(mac):
                raise LabratError("bad_request", f"{where}: reservation {name}: mac is six hex pairs, such as 08:00:11:00:00:0a")
            if any(m == mac for m, _ in out.values()):
                raise LabratError("bad_request", f"{where}: {mac} is reserved twice")
            addr = spec.get("address")
            if addr is not None:
                try:
                    ip = ipaddress.ip_address(str(addr))
                except ValueError:
                    raise LabratError("bad_request", f"{where}: reservation {name}: {addr} is not an address") from None
                if ip not in self.address.network or ip in (self.address.network.network_address, self.address.network.broadcast_address):
                    raise LabratError("bad_request", f"{where}: reservation {name}: {ip} is outside {self.address.network}")
                if str(ip) in taken:
                    raise LabratError("bad_request", f"{where}: reservation {name}: {ip} is already used")
                taken.add(str(ip))
            out[name] = (mac, str(addr) if addr is not None else "")
            given.append(name)
        hosts = (str(h) for h in list(self.address.network.hosts())[9:])     # from .10, as NetworkManager's own range
        for name in given:
            mac, addr = out[name]
            if not addr:
                addr = next((h for h in hosts if h not in taken), "")
                if not addr:
                    raise LabratError("bad_request", f"{where}: no free address left for {name}")
                taken.add(addr)
                out[name] = (mac, addr)
        return out

    # -- the files ---------------------------------------------------------------------------------------------
    def password(self, create: bool = False) -> str:
        if self.psk_path.is_file():
            return self.psk_path.read_text(encoding="utf-8").strip()
        if not create:
            return ""
        psk = "".join(secrets.choice(ALPHABET) for _ in range(16))
        self.psk_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.psk_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(psk + "\n")
        return psk

    def connection(self, psk: str) -> str:
        """The NetworkManager keyfile."""
        lines = ["[connection]", f"id={self.name}", f"uuid={uuid.uuid5(uuid.NAMESPACE_DNS, self.name + '.labrat')}",
                 f"type={'wifi' if self.wifi else 'ethernet'}", f"interface-name={self.interface}", "autoconnect=true", ""]
        if self.wifi:
            lines += ["[wifi]", "mode=ap", f"ssid={self.ssid}", f"band={self.band}"]
            lines += [f"channel={self.channel}"] if self.channel else []
            lines += ["", "[wifi-security]", "key-mgmt=wpa-psk", "proto=rsn", "pairwise=ccmp", "group=ccmp", f"psk={psk}", ""]
        else:
            lines += ["[ethernet]", ""]
        lines += ["[ipv4]", f"address1={self.address.with_prefixlen}", "method=shared", "", "[ipv6]", "method=disabled", ""]
        return "\n".join(lines)

    def dnsmasq(self) -> str:
        """Address reservations for NetworkManager's DHCP server."""
        if not self.reserve:
            return ""
        return f"# {self.name}, written by labrat network up\n" + "".join(
            f"dhcp-host={mac},{addr},{name}\n" for name, (mac, addr) in self.reserve.items())

    @property
    def table(self) -> str:
        return "labrat_" + re.sub(r"[^A-Za-z0-9_]", "_", self.name.removeprefix("labrat-"))

    def dispatcher(self) -> str:
        """A NetworkManager hook that stops traffic crossing this computer to or from the network."""
        if self.internet:
            return ""
        return (f"#!/bin/sh\n# {self.name}, written by labrat network up: the devices on {self.interface} reach this\n"
                f"# computer only.\n[ \"$1\" = \"{self.interface}\" ] || exit 0\n"
                f"case \"$2\" in\n"
                f"  up|pre-up)\n"
                f"    nft delete table inet {self.table} 2>/dev/null\n"
                f"    nft -f - <<'EOF'\n"
                f"table inet {self.table} {{\n"
                f"  chain forward {{\n"
                f"    type filter hook forward priority -10; policy accept;\n"
                f"    iifname \"{self.interface}\" drop\n"
                f"    oifname \"{self.interface}\" drop\n"
                f"  }}\n}}\nEOF\n"
                f"    ;;\n"
                f"  down) nft delete table inet {self.table} 2>/dev/null ;;\n"
                f"esac\nexit 0\n")

    def paths(self) -> dict[str, Path]:
        return {"connection": NM / "system-connections" / f"{self.name}.nmconnection",
                "dnsmasq": NM / "dnsmasq-shared.d" / f"{self.name}.conf",
                "dispatcher": NM / "dispatcher.d" / f"90-{self.name}"}

    def script(self, psk: str) -> str:
        """The shell script that `up` runs as root."""
        p = self.paths()
        out = ["set -e", "umask 077"]

        def put(path: Path, text: str, mode: str) -> None:
            out.extend([f"mkdir -p {path.parent}", f"cat > {path} <<'LABRAT_EOF'", text.rstrip("\n"), "LABRAT_EOF",
                        f"chmod {mode} {path}"])
        put(p["connection"], self.connection(psk), "600")
        if self.dnsmasq():
            put(p["dnsmasq"], self.dnsmasq(), "644")
        else:
            out.append(f"rm -f {p['dnsmasq']}")
        if self.dispatcher():
            put(p["dispatcher"], self.dispatcher(), "755")
            out.append(f"{p['dispatcher']} {self.interface} pre-up")
        else:
            out += [f"rm -f {p['dispatcher']}", f"nft delete table inet {self.table} 2>/dev/null || true"]
        out += ["nmcli connection reload", f"nmcli connection up {self.name}"]
        return "\n".join(out) + "\n"

    def down_script(self) -> str:
        p = self.paths()
        return "\n".join([f"nmcli connection delete {self.name} 2>/dev/null || true",
                          f"rm -f {p['connection']} {p['dnsmasq']} {p['dispatcher']}",
                          f"nft delete table inet {self.table} 2>/dev/null || true",
                          "nmcli connection reload"]) + "\n"

    # -- checks ------------------------------------------------------------------------------------------------
    def check(self, host: Host) -> tuple[list[str], list[str]]:
        """What would stop `up` (problems) and what to know first (notes)."""
        problems, notes = [], []
        if not host.which("nmcli"):
            problems.append("NetworkManager (nmcli) is not installed; it runs the network")
            return problems, notes
        if not self.internet and not host.which("nft"):
            problems.append("nft is not installed; it keeps the devices from reaching beyond this computer "
                            "(install nftables, or set internet = true)")
        sys_net = Path("/sys/class/net") / self.interface
        if not host.exists(sys_net):
            problems.append(f"there is no interface {self.interface} (plugged in? 'ip link' lists them)")
            return problems, notes
        wireless = host.exists(sys_net / "wireless") or host.exists(sys_net / "phy80211")
        if self.wifi and not wireless:
            problems.append(f"{self.interface} is not a Wi-Fi interface; leave out ssid for Ethernet")
        if not self.wifi and wireless:
            problems.append(f"{self.interface} is a Wi-Fi interface; give an ssid to run an access point on it")
        if self.wifi and wireless:
            phy = host.read(sys_net / "phy80211" / "name").strip()
            info = host.run(["iw", "phy", phy, "info"]) if phy and host.which("iw") else ""
            if info and not re.search(r"Supported interface modes:(?:\n\s+\*.*)*\n\s+\* AP\n", info):
                problems.append(f"{self.interface} cannot be an access point; a USB Wi-Fi adapter that supports AP mode can")
            elif not info:
                notes.append(f"could not check that {self.interface} can be an access point (is iw installed?)")
        if self.interface in host.default_route_devices():
            problems.append(f"{self.interface} carries this computer's own connection; the network needs a port of its own")
        state = host.device_state(self.interface)
        if state == "unmanaged":
            problems.append(f"NetworkManager does not manage {self.interface} (see 'nmcli device')")
        active = host.device_connection(self.interface)
        if active and active != self.name:
            notes.append(f"{self.interface} now runs the connection {active!r}; up replaces it")
        for dev, net in host.networks():
            if dev != self.interface and net.overlaps(self.address.network):
                problems.append(f"{self.address.network} overlaps {net} on {dev}; choose another address")
        return problems, notes


class Host:
    """This computer, as the checks and status see it. Tests replace it."""

    def which(self, cmd: str) -> bool:
        return shutil.which(cmd) is not None

    def exists(self, path: Path) -> bool:
        return path.exists()

    def read(self, path: Path) -> str:
        try:
            return path.read_text(encoding="utf-8")
        except OSError:
            return ""

    def run(self, args: list[str]) -> str:
        try:
            return subprocess.run(args, capture_output=True, text=True, timeout=10).stdout
        except (OSError, subprocess.TimeoutExpired):
            return ""

    def _json(self, args: list[str]) -> list:
        try:
            return json.loads(self.run(args) or "[]")
        except ValueError:
            return []

    def default_route_devices(self) -> set[str]:
        return {r.get("dev", "") for r in self._json(["ip", "-j", "route", "show", "default"])}

    def networks(self) -> list[tuple[str, ipaddress.IPv4Network]]:
        out = []
        for link in self._json(["ip", "-j", "-4", "addr"]):
            for a in link.get("addr_info", []):
                try:
                    out.append((link.get("ifname", ""), ipaddress.ip_interface(f"{a['local']}/{a['prefixlen']}").network))
                except (KeyError, ValueError):
                    pass
        return out

    def _device(self, dev: str, field: str) -> str:
        for line in self.run(["nmcli", "-t", "-f", f"DEVICE,{field}", "device"]).splitlines():
            name, _, value = line.partition(":")
            if name == dev:
                return value
        return ""

    def device_state(self, dev: str) -> str:
        return self._device(dev, "STATE")

    def device_connection(self, dev: str) -> str:
        return self._device(dev, "CONNECTION")

    def neighbours(self, dev: str) -> list[dict]:
        return self._json(["ip", "-j", "neigh", "show", "dev", dev])

    def leases(self, dev: str) -> str:
        return self.read(Path("/var/lib/NetworkManager") / f"dnsmasq-{dev}.leases")


def status(net: Network, host: Host | None = None) -> dict:
    """The network as it is now: whether it runs, and the devices on it."""
    host = host or Host()
    active = host.device_connection(net.interface) == net.name
    clients: dict[str, dict] = {}
    names = {mac: name for name, (mac, _) in net.reserve.items()}
    for line in host.leases(net.interface).splitlines():          # expiry mac ip hostname client-id
        parts = line.split()
        if len(parts) >= 4:
            clients[parts[1].lower()] = {"mac": parts[1].lower(), "address": parts[2],
                                         "name": parts[3] if parts[3] != "*" else ""}
    for n in host.neighbours(net.interface) if active else []:
        mac = str(n.get("lladdr", "")).lower()
        if mac and n.get("dst") and not str(n.get("dst")).startswith("fe80"):
            c = clients.setdefault(mac, {"mac": mac, "address": n["dst"], "name": ""})
            c["present"] = "REACHABLE" in n.get("state", []) or "STALE" in n.get("state", []) or "DELAY" in n.get("state", [])
    for mac, c in clients.items():
        if mac in names:
            c["name"] = names[mac]
    out = {"connection": net.name, "interface": net.interface, "kind": "wifi" if net.wifi else "ethernet",
           "address": str(net.address.ip), "network": str(net.address.network), "internet": net.internet,
           "running": active, "clients": sorted(clients.values(), key=lambda c: ipaddress.ip_address(c["address"])),
           "reserved": {name: {"mac": mac, "address": addr} for name, (mac, addr) in net.reserve.items()}}
    if net.wifi:
        out["ssid"] = net.ssid
    if net.port:
        out["url"] = f"http://{net.address.ip}:{net.port}"
    return out
