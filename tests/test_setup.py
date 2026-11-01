import json
import threading
import time

import pytest
from conftest import ADMIN, OPERATOR, VIEWER

from labrat import cli
from labrat.client import Client
from labrat.joins import proof
from labrat.message import LabratError
from labrat.setup import Places, service_text
from labrat.worker import Worker, load_config

KEY = "k" * 43


def ask(url, name="lab-pc", roles=("worker",), **extra):
    from labrat.setup import post
    return post(url, {"action": "ask", "name": name, "roles": list(roles), **{f"{r}_key": KEY for r in roles}, **extra})


def test_a_computer_asks_and_a_person_approves_its_code(nest):
    hub, http = nest
    r = ask(http.url, shared=True)
    assert r["state"] == "waiting" and len(r["code"]) == 9 and "worker_key" not in json.dumps(r)
    olu = Client(http.url, OPERATOR)
    assert olu.joins()[0]["code"] == r["code"]
    with pytest.raises(LabratError) as e:                       # it asked to be shared: an admin decides that
        olu.approve_join(r["code"])
    assert e.value.code == "forbidden"
    with pytest.raises(LabratError):
        Client(http.url, VIEWER).approve_join(r["code"])
    done = olu.approve_join(r["code"], shared=False)
    assert done["owner"] == "olu" and hub.workers.entries["lab-pc"]["shared"] == "no"
    from labrat.setup import post
    with pytest.raises(LabratError):                            # only the computer that asked can follow its request
        post(http.url, {"action": "status", "id": r["id"], "proof": "x"})
    assert post(http.url, {"action": "status", "id": r["id"], "proof": proof(KEY, r["id"])})["state"] == "approved"


def test_an_admin_approves_for_someone_else_and_shares(nest):
    hub, http = nest
    r = ask(http.url, name="gpu-box")
    with pytest.raises(LabratError):
        Client(http.url, OPERATOR).approve_join(r["code"], owner="ada")
    done = Client(http.url, ADMIN).approve_join(r["code"].lower(), owner="olu", shared=True)
    assert done["owner"] == "olu" and hub.workers.entries["gpu-box"]["shared"] == "yes"


def test_denied_expired_and_too_many(nest, monkeypatch):
    hub, http = nest
    r = ask(http.url)
    Client(http.url, OPERATOR).deny_join(r["code"])
    with pytest.raises(LabratError):
        Client(http.url, OPERATOR).approve_join(r["code"])
    for i in range(3):                                          # the denied one no longer counts
        ask(http.url, name=f"pc{i}")
    with pytest.raises(LabratError) as e:                       # three from one address are waiting already
        ask(http.url, name="pc9")
    assert e.value.code == "unavailable"
    for req in hub.joins.waiting.values():
        req["_expires"] = time.time() - 1
    assert Client(http.url, OPERATOR).joins() == []


def test_join_keys_let_computers_in_at_once(nest):
    hub, http = nest
    with pytest.raises(LabratError):
        Client(http.url, OPERATOR).new_join_key()
    admin = Client(http.url, ADMIN)
    k = admin.new_join_key(uses=2, hours=1, shared=True, owner="olu")
    assert k["key"].startswith("lk_") and k["key"] not in (hub.joins.path.read_text())
    r = ask(http.url, name="pi-1", join_key=k["key"])
    assert r["state"] == "approved" and r["owner"] == "olu" and hub.workers.entries["pi-1"]["shared"] == "yes"
    with pytest.raises(LabratError) as e:
        ask(http.url, name="pi-2", roles=("worker", "rat"), rat_port=8781, join_key=k["key"])
    assert "worker only" in e.value.message
    ask(http.url, name="pi-3", join_key=k["key"])
    with pytest.raises(LabratError) as e:
        ask(http.url, name="pi-4", join_key=k["key"])
    assert "run out" in e.value.message
    k2 = admin.new_join_key()
    admin.revoke_join_key(k2["id"])
    with pytest.raises(LabratError):
        ask(http.url, name="pi-5", join_key=k2["key"])


def test_a_rat_joins_and_is_adopted(nest, rat):
    hub, http = nest
    rat_hub, rat_url = rat
    from labrat.setup import post
    r = post(http.url, {"action": "ask", "name": "bench-rat", "roles": ["rat"], "rat_key": rat_hub.key,
                        "rat_port": int(rat_url.rsplit(":", 1)[1])})
    with pytest.raises(LabratError):                            # a rat needs an admin
        Client(http.url, OPERATOR).approve_join(r["code"])
    Client(http.url, ADMIN).approve_join(r["code"])
    assert "bench-rat" in hub.rats and any(d["device"] == "bench-rat/psu" for d in Client(http.url, ADMIN).devices())


def setup_args(url, root, *extra):
    return ["setup", "--yes", "--fast", "--no-benchmark", "--url", url, "--root", str(root), "--no-service", "--role", "worker",
            "--name", "lap", *extra]


def test_setup_with_a_join_key_writes_joins_and_undoes(nest, tmp_path, capsys):
    hub, http = nest
    key = Client(http.url, ADMIN).new_join_key(owner="olu")["key"]
    assert cli.main(setup_args(http.url, tmp_path, "--key", key, "--cpus", "1", "--store", f"nas={tmp_path}")) == 0
    out = capsys.readouterr().out
    assert "Approved by join key" in out and "Installed" in out
    places = Places(tmp_path)
    cfg = load_config(places.config("worker"))
    assert cfg["name"] == "lap" and cfg["cpus"] == 1 and cfg["stores"]["nas"]["path"] == str(tmp_path)
    assert Worker(cfg).hello()["owner"] == "olu"                # it can talk to the nest at once
    assert cli.main(["setup", "--undo", "--yes", "--root", str(tmp_path)]) == 0
    assert "lap" not in hub.workers.entries and not places.config("worker").exists() and not places.record.exists()


def test_setup_waits_for_a_person_to_approve_its_code(nest, tmp_path):
    hub, http = nest
    result = {}
    # a daemon thread: if anything here fails, setup's wait for approval must not keep the test run from ending
    t = threading.Thread(target=lambda: result.setdefault("code", cli.main(setup_args(http.url, tmp_path))), daemon=True)
    t.start()
    olu, deadline = Client(http.url, OPERATOR), time.time() + 60
    while not olu.joins() and time.time() < deadline and t.is_alive():
        time.sleep(0.1)
    waiting = olu.joins()
    assert waiting, f"setup never asked to join (it returned {result.get('code')})"
    assert cli.main(["approve", waiting[0]["code"], "--url", http.url, "--token", OPERATOR]) == 0
    t.join(60)
    assert result["code"] == 0 and hub.workers.entries["lap"]["owner"] == "olu"


def test_a_dry_run_changes_nothing(nest, tmp_path):
    hub, http = nest
    assert cli.main(setup_args(http.url, tmp_path, "--dry-run")) == 0
    assert not (tmp_path / "config").exists() and not hub.joins.waiting


def test_service_files(tmp_path):
    places = Places(tmp_path)
    places.mac, places.system = False, False
    unit = service_text(places, "worker")
    assert "labrat.cli worker --config" in unit and "WantedBy=default.target" in unit
    places.mac = True
    plist = service_text(places, "worker")
    assert "<string>org.labrat.worker</string>" in plist and "KeepAlive" in plist


def test_usb_devices_on_macos_come_from_the_io_registry():
    import plistlib

    from labrat.detect import mac_usb_devices
    tree = plistlib.dumps([{"idVendor": 0x0699, "idProduct": 0x0346, "USB Product Name": "AFG3252", "USB Vendor Name": "Tektronix",
                            "IORegistryEntryChildren": [{"IORegistryEntryChildren": [{"IOCalloutDevice": "/dev/cu.usbmodem1101"}]},
                                                        {"idVendor": 1, "idProduct": 2, "USB Product Name": "USB2.0 Hub"}]}])
    found = mac_usb_devices(tree)
    assert found == [{"usb": "0699:0346", "manufacturer": "Tektronix", "product": "AFG3252", "serial": "", "bus": "",
                      "address": "", "files": ["/dev/cu.usbmodem1101"]}]
    assert mac_usb_devices(b"") == []


def test_long_values_are_shortened_not_wrapped():
    from labrat.setup import Ui
    ui = Ui(interactive=False)
    long = "/private/tmp/" + "x" * 80 + "/scratchpad/trial/config"
    short = ui.fit(long, 40)
    assert len(short) == 40 and short.startswith("/private") and short.endswith("trial/config") and "…" in short


def test_the_hardware_is_described_and_shared_only_when_asked(nest, tmp_path):
    from labrat.hardware import describe, disk_speed, lines
    hw = describe()
    assert hw["arch"] and hw["cpu"]["cores"] >= 1 and hw["disks"] and "serial" not in str(hw).lower()
    assert disk_speed(tmp_path, mb=8) and not list(tmp_path.glob(".labrat-speed*"))
    text = dict(lines({"model": "Raspberry Pi 5 Model B Rev 1.0", "arch": "aarch64", "cpu": {"model": "Cortex-A76", "cores": 4, "ghz": 2.4},
                       "memory": {"gb": 8}, "disks": [{"size_gb": 64, "kind": "SD card", "system": True, "free_gb": 40, "write_mbps": 45}],
                       "network": [{"name": "Ethernet", "mbps": 1000}]}))
    assert text["Computer"] == "Raspberry Pi 5 Model B Rev 1.0, aarch64" and text["Processor"] == "Cortex-A76, 4 cores (2.4 GHz)"
    assert text["Disk"] == "64 GB SD card, 40 GB free, writes 45 MB/s" and text["Network"] == "Ethernet, 1 Gb/s"
    hub, http = nest
    key = Client(http.url, ADMIN).new_join_key(uses=2, owner="olu")["key"]
    assert cli.main(setup_args(http.url, tmp_path / "a", "--key", key, "--keep-hardware")) == 0
    Worker(load_config(Places(tmp_path / "a").config("worker"))).hello()
    assert "hardware" not in hub.workers.report("lap")
    assert cli.main([*setup_args(http.url, tmp_path / "b", "--key", key, "--share-hardware"), "--name", "lap2"]) == 0
    Worker(load_config(Places(tmp_path / "b").config("worker"))).hello()
    assert hub.workers.report("lap2")["hardware"]["cpu"]["cores"] >= 1
