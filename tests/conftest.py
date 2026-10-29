import json
import urllib.request

import pytest

from labrat import config as conf
from labrat.http import Server
from labrat.hub import token_hash
from labrat.nest import Nest
from labrat.rat import Rat

ADMIN, OPERATOR, VIEWER, OTHER = "admin-token", "operator-token", "viewer-token", "other-token"

TOKENS = f'''
[[tokens]]
name = "ada"
role = "admin"
sha256 = "{token_hash(ADMIN)}"
[[tokens]]
name = "olu"
role = "operator"
sha256 = "{token_hash(OPERATOR)}"
[[tokens]]
name = "vic"
role = "viewer"
sha256 = "{token_hash(VIEWER)}"
[[tokens]]
name = "oth"
role = "operator"
sha256 = "{token_hash(OTHER)}"
'''

SIM = '''
[devices.psu]
driver = "sim-supply"
[devices.psu.limits.VoltageLevel]
max = 12
[devices.relays]
driver = "sim-gpio"
outputs = { heater = 17 }
inputs = { door = 22 }
safe = { heater = false }
'''


def load(tmp_path, kind, text):
    p = tmp_path / f"{kind}.toml"
    p.write_text(f'host = "127.0.0.1"\nport = 0\ndiscovery = false\nstate_dir = "{tmp_path / kind}"\n' + text)
    return conf.load(kind, p)


class Http:
    def __init__(self, url):
        self.url = url

    def post(self, body, token=ADMIN):
        req = urllib.request.Request(self.url + "/api/v1/message", data=json.dumps(body).encode(), method="POST",
                                     headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def get(self, path, token=ADMIN):
        req = urllib.request.Request(self.url + path, headers={"Authorization": f"Bearer {token}"} if token else {})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())


@pytest.fixture
def nest(tmp_path):
    hub = Nest(load(tmp_path, "nest", TOKENS + SIM))
    server = Server(hub, "127.0.0.1", 0).start()
    yield hub, Http(f"http://127.0.0.1:{server.port}")
    server.close()
    hub.close()


@pytest.fixture
def rat(tmp_path):
    hub = Rat(load(tmp_path, "rat", 'name = "bench-rat"\nwatchdog_s = 0\n' + SIM))
    server = Server(hub, "127.0.0.1", 0).start()
    yield hub, f"http://127.0.0.1:{server.port}"
    server.close()
    hub.close()
