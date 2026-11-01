import json
import socket
import threading
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


class Listener:
    """A localhost TCP server for a fake device, handling each connection in a thread with handle(conn).

    close() stops the thread before it closes the socket. A thread still waiting in accept() on a socket closed
    under it can end up accepting on whatever socket is opened next with the same file number, such as the next
    test's nest, and take its connections."""

    def __init__(self, handle):
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen()
        self.port = self.sock.getsockname()[1]
        self._stop = threading.Event()
        self.thread = threading.Thread(target=self._serve, args=(handle,), daemon=True)
        self.thread.start()

    def _serve(self, handle):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            if self._stop.is_set():
                conn.close()
                return
            with conn:
                handle(conn)

    def getsockname(self):
        return self.sock.getsockname()

    def close(self):
        self._stop.set()
        try:                                                  # wakes the accept() so the thread can see it is done
            socket.create_connection(("127.0.0.1", self.port), timeout=2).close()
        except OSError:
            pass
        self.thread.join(5)
        self.sock.close()


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
