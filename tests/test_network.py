import ipaddress
from pathlib import Path

import pytest

from labrat import cli
from labrat import config as conf
from labrat.message import LabratError
from labrat.network import Host, Network, status

IW_AP = "Wiphy phy0\n\tSupported interface modes:\n\t\t * managed\n\t\t * AP\n\t\t * monitor\n\tBand 1:\n"
IW_NO_AP = "Wiphy phy1\n\tSupported interface modes:\n\t\t * managed\n\t\t * AP/VLAN\n\t\t * monitor\n\tBand 1:\n"


class FakeHost(Host):
    def __init__(self, links=("eth0", "wlan1"), wireless=("wlan1",), iw=IW_AP, default=("eth0",),
                 nets=(("eth0", "198.51.100.0/24"),), tools=("nmcli", "nft", "iw"), states=None, conns=None,
                 neigh=(), leases=""):
        self.links, self.wireless, self.iw, self.default = set(links), set(wireless), iw, set(default)
        self.nets = [(d, ipaddress.ip_network(n)) for d, n in nets]
        self.tools, self.states, self.conns = set(tools), states or {}, conns or {}
        self.neigh, self.lease_text = list(neigh), leases

    def which(self, cmd):
        return cmd in self.tools

    def exists(self, path):
        parts = Path(path).parts
        if parts[:4] != ("/", "sys", "class", "net"):
            return False
        dev = parts[4]
        return dev in self.links and (len(parts) == 5 or dev in self.wireless)

    def read(self, path):
        return "phy0\n" if str(path).endswith("phy80211/name") else ""

    def run(self, args):
        return self.iw if args[:2] == ["iw", "phy"] else ""

    def default_route_devices(self):
        return self.default

    def networks(self):
        return self.nets

    def device_state(self, dev):
        return self.states.get(dev, "disconnected")

    def device_connection(self, dev):
        return self.conns.get(dev, "")

    def neighbours(self, dev):
        return self.neigh

    def leases(self, dev):
        return self.lease_text


def cfg(tmp_path, **net):
    return {"name": "bench", "port": 8780, "state_dir": tmp_path, "network": {"interface": "wlan1", **net}}


def test_wifi_files(tmp_path):
    n = Network(cfg(tmp_path, ssid="labrat-bench", channel=6, reserve={"afg": "08-00-11-00-00-0A",
                                                                       "scope": {"mac": "08:00:11:22:33:44", "address": "10.42.0.10"}}))
    k = n.connection("secretpass23")
    assert "type=wifi" in k and "mode=ap" in k and "ssid=labrat-bench" in k and "channel=6" in k
    assert "key-mgmt=wpa-psk" in k and "proto=rsn" in k and "psk=secretpass23" in k
    assert "address1=10.42.0.1/24" in k and "method=shared" in k
    # an explicit address is kept, and the automatic one skips it
    assert n.reserve == {"afg": ("08:00:11:00:00:0a", "10.42.0.11"), "scope": ("08:00:11:22:33:44", "10.42.0.10")}
    assert "dhcp-host=08:00:11:00:00:0a,10.42.0.11,afg" in n.dnsmasq()
    d = n.dispatcher()
    assert 'iifname "wlan1" drop' in d and 'oifname "wlan1" drop' in d and "table inet labrat_bench" in d
    s = n.script("secretpass23")
    assert s.index("dispatcher.d/90-labrat-bench wlan1 pre-up") < s.index("nmcli connection up labrat-bench")
    assert "chmod 600 /etc/NetworkManager/system-connections/labrat-bench.nmconnection" in s


def test_ethernet_with_internet_writes_no_firewall(tmp_path):
    n = Network(cfg(tmp_path, interface="eth1", internet=True))
    assert "type=ethernet" in n.connection("") and "wifi" not in n.connection("")
    assert n.dispatcher() == "" and n.dnsmasq() == ""
    s = n.script("")
    assert "rm -f /etc/NetworkManager/dispatcher.d/90-labrat-bench" in s
    assert "nft delete table inet labrat_bench" in s and "rm -f /etc/NetworkManager/dnsmasq-shared.d/labrat-bench.conf" in s
    assert "nmcli connection delete labrat-bench" in n.down_script()


@pytest.mark.parametrize("net, words", [
    ({"interface": "wlan 1"}, "interface"),
    ({"bogus": 1}, "unknown"),
    ({"channel": 6}, "needs an ssid"),
    ({"ssid": "x", "band": "6"}, "band"),
    ({"address": ".".join(["8"] * 3) + ".1/24"}, "private"),     # a public address
    ({"address": "10.42.0.0/24"}, "private"),
    ({"address": "10.42.0.1/30"}, "at least"),
    ({"internet": "no"}, "true or false"),
    ({"reserve": {"afg": "08:00:11"}}, "six hex pairs"),
    ({"reserve": {"a": "08:00:11:00:00:0a", "b": "08:00:11:00:00:0a"}}, "reserved twice"),
    ({"reserve": {"afg": {"mac": "08:00:11:00:00:0a", "address": "192.0.2.5"}}}, "outside"),
    ({"reserve": {"afg": {"mac": "08:00:11:00:00:0a", "address": "10.42.0.1"}}}, "already used"),
    ({"reserve": {"bad name": "08:00:11:00:00:0a"}}, "host name"),
])
def test_bad_settings(tmp_path, net, words):
    with pytest.raises(LabratError) as e:
        Network({"name": "bench", "state_dir": tmp_path, "network": {"interface": "wlan1", **net}})
    assert words in e.value.message


def test_config_checks_network_at_load(tmp_path):
    p = tmp_path / "nest.toml"
    p.write_text(f'state_dir = "{tmp_path}"\n[network]\ninterface = "eth1"\naddress = "10.42.0.1/31"\n')
    with pytest.raises(LabratError):
        conf.load("nest", p)
    p.write_text(f'state_dir = "{tmp_path}"\n[network]\ninterface = "eth1"\n')
    assert conf.load("nest", p)["network"]["interface"] == "eth1"


def test_password_made_once_and_private(tmp_path):
    n = Network(cfg(tmp_path, ssid="x"))
    assert n.password() == ""
    psk = n.password(create=True)
    assert len(psk) == 16 and n.password(create=True) == psk
    assert (tmp_path / "network-password").stat().st_mode & 0o777 == 0o600


def test_checks(tmp_path):
    wifi = Network(cfg(tmp_path, ssid="x"))
    assert wifi.check(FakeHost()) == ([], [])
    problems, _ = wifi.check(FakeHost(iw=IW_NO_AP))
    assert any("access point" in p for p in problems)
    problems, _ = wifi.check(FakeHost(default=("wlan1",)))
    assert any("own connection" in p for p in problems)
    problems, _ = wifi.check(FakeHost(nets=(("eth0", "10.42.0.0/16"),)))
    assert any("overlaps" in p for p in problems)
    problems, _ = wifi.check(FakeHost(tools=("nmcli", "iw")))
    assert any("nft" in p for p in problems)
    problems, _ = wifi.check(FakeHost(tools=()))
    assert problems == ["NetworkManager (nmcli) is not installed; it runs the network"]
    problems, _ = wifi.check(FakeHost(links=("eth0",)))
    assert any("no interface wlan1" in p for p in problems)
    problems, _ = wifi.check(FakeHost(states={"wlan1": "unmanaged"}))
    assert any("does not manage" in p for p in problems)
    _, notes = wifi.check(FakeHost(conns={"wlan1": "eduroam"}))
    assert any("replaces it" in n for n in notes)
    eth = Network(cfg(tmp_path, interface="wlan1", internet=True))
    problems, _ = eth.check(FakeHost())
    assert any("give an ssid" in p for p in problems)
    problems, _ = Network(cfg(tmp_path, interface="eth0", ssid="x")).check(FakeHost())
    assert any("not a Wi-Fi interface" in p for p in problems)


def test_status_names_clients(tmp_path):
    n = Network(cfg(tmp_path, ssid="x", reserve={"afg": "08:00:11:00:00:0a", "pump": "aa:bb:cc:dd:ee:ff"}))
    host = FakeHost(conns={"wlan1": "labrat-bench"},
                    leases="1798000000 08:00:11:00:00:0a 10.42.0.10 * 01:08\n1798000000 de:ad:be:ef:00:01 10.42.0.57 esp32-rat *\n",
                    neigh=[{"dst": "10.42.0.10", "lladdr": "08:00:11:00:00:0a", "state": ["REACHABLE"]},
                           {"dst": "10.42.0.57", "lladdr": "de:ad:be:ef:00:01", "state": ["FAILED"]}])
    s = status(n, host)
    assert s["running"] and s["url"] == "http://10.42.0.1:8780" and s["ssid"] == "x"
    assert [(c["address"], c["name"], c.get("present")) for c in s["clients"]] == [
        ("10.42.0.10", "afg", True), ("10.42.0.57", "esp32-rat", False)]
    assert set(s["reserved"]) == {"afg", "pump"}
    assert status(n, FakeHost())["running"] is False


def test_cli_plan_hides_password_and_status(tmp_path, capsys, monkeypatch):
    p = tmp_path / "nest.toml"
    p.write_text(f'name = "bench"\nstate_dir = "{tmp_path}"\n[network]\ninterface = "wlan1"\nssid = "labrat-bench"\n')
    monkeypatch.setattr("labrat.network.Host", FakeHost)
    Network(conf.load("nest", p)).password(create=True)
    psk = (tmp_path / "network-password").read_text().strip()
    assert cli.main(["network", "plan", "--config", str(p)]) == 0
    out = capsys.readouterr().out
    assert "psk=********" in out and psk not in out
    assert cli.main(["network", "--config", str(p)]) == 0
    assert psk not in capsys.readouterr().out
    cli.main(["network", "--config", str(p), "--show-password"])
    assert psk in capsys.readouterr().out


def test_cli_up_refuses_problems_and_runs_with_sudo(tmp_path, capsys, monkeypatch):
    p = tmp_path / "nest.toml"
    p.write_text(f'name = "bench"\nstate_dir = "{tmp_path}"\n[network]\ninterface = "eth9"\n')
    monkeypatch.setattr("labrat.network.Host", FakeHost)
    assert cli.main(["network", "up", "--config", str(p), "--yes"]) == 2      # no eth9
    p.write_text(f'name = "bench"\nstate_dir = "{tmp_path}"\n[network]\ninterface = "wlan1"\nssid = "s"\n')
    ran = []
    monkeypatch.setattr("subprocess.run", lambda cmd, input, text: ran.append((cmd, input)) or type("R", (), {"returncode": 0})())
    monkeypatch.setattr("os.geteuid", lambda: 1000)
    assert cli.main(["network", "up", "--config", str(p), "--yes"]) == 0
    cmd, script = ran[0]
    assert cmd == ["sudo", "sh", "-s"] and "psk=" + (tmp_path / "network-password").read_text().strip() in script
    monkeypatch.setattr("builtins.input", lambda prompt: "n")
    assert cli.main(["network", "down", "--config", str(p)]) == 1 and len(ran) == 1
