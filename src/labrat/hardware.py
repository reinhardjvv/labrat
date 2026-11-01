"""What a computer is: its model, system, processor, memory, GPUs, disks and network links, on Linux (x86 and ARM
boards such as a Raspberry Pi or a Jetson) and on macOS (Apple silicon and Intel).

    from labrat.hardware import describe
    describe()                  # everything below, read without root and in a second or two
    disk_speed(folder)          # a short write test, in MB/s, when asked for

Nothing here identifies the computer itself: no serial numbers, MAC addresses or host names. A worker shares the
result with its lab only when its owner says so (share_hardware in worker.toml), so placement and people can see
what each computer has.
"""
from __future__ import annotations

import json
import os
import platform
import plistlib
import re
import shutil
import subprocess
import time
from pathlib import Path


def _run(args: list[str], timeout: float = 8.0) -> str:
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def _read(path: str) -> str:
    try:
        return Path(path).read_text(errors="replace").strip().strip("\x00")
    except OSError:
        return ""


def _gb(n: float) -> float:
    return round(n / 1e9, 1)


def describe() -> dict:
    """The computer, as a dict of plain values."""
    d = _mac() if platform.system() == "Darwin" else _linux()
    d["arch"] = platform.machine()
    from .worker import gpus
    d["gpus"] = d.get("gpus", []) + [{"name": g["name"], "memory_gb": g["memory_gb"], "kind": "NVIDIA"} for g in gpus()]
    d["network"] = network()
    return {k: v for k, v in d.items() if v not in (None, "", [], {})}


# -- macOS -----------------------------------------------------------------------------------------------------------
def _mac() -> dict:
    d: dict = {"system": f"macOS {_run(['sw_vers', '-productVersion']).strip()}"}
    try:
        sp = json.loads(_run(["system_profiler", "SPHardwareDataType", "SPDisplaysDataType", "SPMemoryDataType", "-json"], 20) or "{}")
    except ValueError:
        sp = {}
    hw = (sp.get("SPHardwareDataType") or [{}])[0]
    chip = hw.get("chip_type") or _run(["sysctl", "-n", "machdep.cpu.brand_string"]).strip()
    d["model"] = hw.get("machine_name", "Mac")
    perf, eff = (_run(["sysctl", "-n", f"hw.perflevel{i}.physicalcpu"]).strip() for i in (0, 1))
    cores = os.cpu_count() or 1
    split = f" ({perf} performance, {eff} efficiency)" if perf.isdigit() and eff.isdigit() and int(eff) else ""
    ghz = _run(["sysctl", "-n", "hw.cpufrequency_max"]).strip()           # Intel Macs only
    d["cpu"] = {"model": chip, "cores": cores, "detail": split.strip(" ()"),
                **({"ghz": round(int(ghz) / 1e9, 2)} if ghz.isdigit() else {})}
    mem = (sp.get("SPMemoryDataType") or [{}])[0]
    size = hw.get("physical_memory") or mem.get("SPMemoryDataType", "")
    d["memory"] = {"gb": float(re.sub(r"[^\d.]", "", size) or 0), **({"type": mem["dimm_type"]} if mem.get("dimm_type") else {})}
    d["gpus"] = [{"name": g.get("sppci_model", g.get("_name", "GPU")), "kind": "integrated" if g.get("sppci_bus") == "spdisplays_builtin" else "",
                  **({"cores": int(g["sppci_cores"])} if str(g.get("sppci_cores", "")).isdigit() else {})}
                 for g in sp.get("SPDisplaysDataType") or [] if g.get("sppci_device_type", "spdisplays_gpu") == "spdisplays_gpu"]
    d["disks"] = _mac_disks()
    return d


def _mac_disks() -> list[dict]:
    disks = []
    try:
        listing = plistlib.loads(_run(["diskutil", "list", "-plist", "physical"]).encode() or b"<plist><dict/></plist>")
    except Exception:
        listing = {}
    for name in listing.get("WholeDisks") or []:
        try:
            info = plistlib.loads(_run(["diskutil", "info", "-plist", name]).encode())
        except Exception:
            continue
        if not info.get("TotalSize"):
            continue
        disk = {"size_gb": _gb(info["TotalSize"]), "kind": "SSD" if info.get("SolidState") else "HDD",
                "bus": info.get("BusProtocol", ""), "internal": bool(info.get("Internal"))}
        if name == _mac_system_disk():
            disk["system"] = True
            disk["free_gb"] = _gb(shutil.disk_usage("/").free)
        disks.append(disk)
    if not disks:                                              # at least the disk this runs from
        u = shutil.disk_usage("/")
        disks.append({"size_gb": _gb(u.total), "free_gb": _gb(u.free), "kind": "", "system": True})
    return disks


def _mac_system_disk() -> str:
    try:
        info = plistlib.loads(_run(["diskutil", "info", "-plist", "/"]).encode())
    except Exception:
        return ""
    for store in info.get("APFSPhysicalStores") or []:                   # the APFS container's physical disk
        m = re.match(r"(disk\d+)", str(store.get("APFSPhysicalStore", "")))
        if m:
            return m.group(1)
    return re.sub(r"s\d+.*$", "", str(info.get("ParentWholeDisk") or info.get("DeviceIdentifier", "")))


# -- Linux -----------------------------------------------------------------------------------------------------------
def _linux() -> dict:
    d: dict = {}
    osr = dict(re.findall(r'^(\w+)="?([^"\n]*)"?$', _read("/etc/os-release"), re.M))
    d["system"] = osr.get("PRETTY_NAME") or f"Linux {platform.release()}"
    model = _read("/proc/device-tree/model") or " ".join(x for x in (_read("/sys/devices/virtual/dmi/id/sys_vendor"),
                                                                     _read("/sys/devices/virtual/dmi/id/product_name")) if x)
    d["model"] = re.sub(r"\s+", " ", model).strip() or None
    info = _read("/proc/cpuinfo")
    names = re.findall(r"^(?:model name|Model|cpu model|Hardware)\s*:\s*(.+)$", info, re.M)
    cpu_model = names[0].strip() if names else _arm_cpu(info)
    ghz = _read("/sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_max_freq")
    cores = os.cpu_count() or 1
    clusters = {}
    for cap in Path("/sys/devices/system/cpu").glob("cpu[0-9]*/cpu_capacity"):    # big.LITTLE on ARM
        clusters[_read(str(cap))] = clusters.get(_read(str(cap)), 0) + 1
    detail = ", ".join(f"{n} at capacity {c}" for c, n in sorted(clusters.items(), key=lambda x: -int(x[0] or 0))) if len(clusters) > 1 else ""
    d["cpu"] = {"model": cpu_model, "cores": cores, **({"ghz": round(int(ghz) / 1e6, 2)} if ghz.isdigit() else {}),
                **({"detail": detail} if detail else {})}
    mem = re.search(r"MemTotal:\s+(\d+)", _read("/proc/meminfo"))
    d["memory"] = {"gb": round(int(mem.group(1)) / 1024 ** 2, 1)} if mem else {}
    d["disks"] = _linux_disks()
    if Path("/dev/nvhost-gpu").exists() or "tegra" in _read("/proc/device-tree/compatible"):
        d["gpus"] = [{"name": "NVIDIA Tegra (Jetson)", "kind": "integrated"}]
    return d


def _arm_cpu(info: str) -> str:
    """ARM boards name the core design by number in /proc/cpuinfo."""
    parts = {"0xd03": "Cortex-A53", "0xd04": "Cortex-A35", "0xd05": "Cortex-A55", "0xd07": "Cortex-A57", "0xd08": "Cortex-A72",
             "0xd09": "Cortex-A73", "0xd0a": "Cortex-A75", "0xd0b": "Cortex-A76", "0xd0d": "Cortex-A77", "0xd41": "Cortex-A78",
             "0xd44": "Cortex-X1", "0xd46": "Cortex-A510", "0xd47": "Cortex-A710", "0xd4d": "Cortex-A715", "0xd4b": "Cortex-A78C",
             "0xd4e": "Cortex-X3", "0xd80": "Cortex-A520", "0xd81": "Cortex-A720", "0xd82": "Cortex-X4", "0xd85": "Cortex-X925",
             "0xd87": "Cortex-A725", "0xd0c": "Neoverse N1", "0xd40": "Neoverse V1", "0xd4f": "Neoverse V2"}
    found = [parts.get(p.lower(), p) for p in re.findall(r"^CPU part\s*:\s*(\w+)", info, re.M)]
    return " + ".join(sorted(set(found), key=found.index)) or platform.processor() or platform.machine()


def _linux_disks() -> list[dict]:
    try:
        rows = json.loads(_run(["lsblk", "-J", "-b", "-d", "-o", "NAME,SIZE,ROTA,TRAN,TYPE,RM"]) or "{}").get("blockdevices") or []
    except ValueError:
        rows = []
    root = os.stat("/").st_dev
    disks = []
    for r in rows:
        if r.get("type") != "disk" or r.get("name", "").startswith(("loop", "zram", "ram")) or not r.get("size"):
            continue
        tran = (r.get("tran") or "").lower()
        kind = "NVMe SSD" if tran == "nvme" or r["name"].startswith("nvme") else \
            "SD card" if r["name"].startswith("mmcblk") else "HDD" if str(r.get("rota")) in ("1", "True", "true") else "SSD"
        disk = {"size_gb": _gb(int(r["size"])), "kind": kind, "bus": tran.upper() if tran else "", "internal": not r.get("rm")}
        try:
            if any(os.stat(p).st_rdev and os.major(os.stat(p).st_rdev) == os.major(root) for p in [f"/dev/{r['name']}"]):
                disk.update(system=True, free_gb=_gb(shutil.disk_usage("/").free))
        except OSError:
            pass
        disks.append(disk)
    if not any(d.get("system") for d in disks):
        u = shutil.disk_usage("/")
        disks.insert(0, {"size_gb": _gb(u.total), "free_gb": _gb(u.free), "kind": "", "system": True})
    return disks


# -- network ---------------------------------------------------------------------------------------------------------
def network() -> list[dict]:
    """The links that are up: Ethernet with its speed, Wi-Fi, Thunderbolt and so on. No addresses."""
    links = []
    if platform.system() == "Darwin":
        ports = re.findall(r"Hardware Port: (.+)\nDevice: (\w+)", _run(["networksetup", "-listallhardwareports"]))
        for name, dev in ports:
            ifc = _run(["ifconfig", dev])
            if "status: active" not in ifc:
                continue
            m = re.search(r"media: .*?\((\d+)(G|)base", ifc)
            speed = (int(m.group(1)) * (1000 if m.group(2) else 1)) if m else None
            links.append({"name": "Wi-Fi" if "Wi-Fi" in name else re.sub(r"\s*\(.*\)$", "", name),
                          **({"mbps": speed} if speed else {})})
        return links
    for net in sorted(Path("/sys/class/net").glob("*")):
        if net.name == "lo" or _read(str(net / "operstate")) != "up" or not (net / "device").exists():
            continue
        speed = _read(str(net / "speed"))
        wifi = (net / "wireless").exists() or (net / "phy80211").exists()
        links.append({"name": "Wi-Fi" if wifi else "Ethernet", **({"mbps": int(speed)} if speed.isdigit() and int(speed) > 0 else {})})
    return links


# -- speed -----------------------------------------------------------------------------------------------------------
def disk_speed(folder: Path, mb: int = 256) -> float | None:
    """Write mb megabytes to a file in folder, flushed to the disk, and return MB/s. The file is removed after."""
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f".labrat-speed-{os.getpid()}"
    block = os.urandom(4 << 20)
    try:
        t0 = time.perf_counter()
        with open(path, "wb", buffering=0) as f:
            for _ in range(mb // 4):
                f.write(block)
            os.fsync(f.fileno())
        took = time.perf_counter() - t0
        return round(mb / took) if took > 0 else None
    except OSError:
        return None
    finally:
        path.unlink(missing_ok=True)


# -- in words --------------------------------------------------------------------------------------------------------
def lines(d: dict) -> list[tuple[str, str]]:
    """(label, text) rows describing the computer, for setup and labrat worker show."""
    out = []
    if d.get("model"):
        out.append(("Computer", f"{d['model']}, {d.get('arch', '')}".rstrip(", ")))
    if d.get("system"):
        out.append(("System", d["system"]))
    c = d.get("cpu") or {}
    if c:
        extra = ", ".join(x for x in (f"{c['ghz']:g} GHz" if c.get("ghz") else "", c.get("detail", "")) if x)
        out.append(("Processor", f"{c.get('model', '')}, {c.get('cores', '?')} cores" + (f" ({extra})" if extra else "")))
    m = d.get("memory") or {}
    if m.get("gb"):
        out.append(("Memory", f"{m['gb']:g} GB" + (f" {m['type']}" if m.get("type") else "")))
    for g in d.get("gpus") or []:
        bits = [f"{g['cores']} cores" if g.get("cores") else "", f"{g['memory_gb']:g} GB" if g.get("memory_gb") else ""]
        out.append(("GPU", g["name"] + (f", {', '.join(b for b in bits if b)}" if any(bits) else "")))
    for disk in d.get("disks") or []:
        what = " ".join(x for x in (f"{disk['size_gb']:g} GB", disk.get("kind", ""), f"({disk['bus']})" if disk.get("bus") else "") if x)
        if disk.get("free_gb") is not None:
            what += f", {disk['free_gb']:g} GB free"
        if disk.get("write_mbps"):
            what += f", writes {disk['write_mbps'] / 1000:.1f} GB/s" if disk["write_mbps"] >= 1000 else f", writes {disk['write_mbps']} MB/s"
        out.append(("Disk" + (" (system)" if disk.get("system") and len(d["disks"]) > 1 else ""), what))
    for link in d.get("network") or []:
        speed = link.get("mbps")
        out.append(("Network", link["name"] + (f", {speed / 1000:g} Gb/s" if speed and speed >= 1000 else f", {speed} Mb/s" if speed else "")))
    return out
