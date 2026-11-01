import json
import urllib.request

import pytest
from conftest import ADMIN, OPERATOR, OTHER, VIEWER

from labrat import signing
from labrat.message import LabratError
from labrat.worker import Worker, check_config, check_url, in_hours


def make_worker(tmp_path, url, name="gpu-box", **cfg):
    return Worker(check_config({"name": name, "state_dir": str(tmp_path / f"worker-{name}"), "nest": url, **cfg}))


def test_a_person_joins_their_own_computer(nest, tmp_path):
    hub, http = nest
    w = make_worker(tmp_path, http.url)
    r = w.join(http.url, OPERATOR)
    assert r["owner"] == "olu" and r["shared"] == "no" and r["name"] == "gpu-box"
    assert hub.workers.entries["gpu-box"]["key"] == w.key
    assert hub.workers.allows("gpu-box", "olu") and not hub.workers.allows("gpu-box", "oth")
    assert json.loads((tmp_path / "worker-gpu-box" / "worker.json").read_text())["owner"] == "olu"


def test_joining_needs_a_person_with_an_operator_token(nest, tmp_path):
    hub, http = nest
    w = make_worker(tmp_path, http.url)
    with pytest.raises(LabratError) as e:
        w.join(http.url, VIEWER)
    assert e.value.code == "forbidden"
    _, r = http.post({"op": "users", "name": "claude-olu", "args": {"action": "add", "role": "operator", "for": "olu"}})
    with pytest.raises(LabratError) as e:
        w.join(http.url, r["value"]["token"])
    assert e.value.code == "forbidden" and "person" in e.value.message
    w.join(http.url, OPERATOR)
    with pytest.raises(LabratError) as e:                        # someone else cannot take over the name
        make_worker(tmp_path / "x", http.url).join(http.url, OTHER)
    assert "already a worker" in e.value.message


def test_sharing_needs_the_owner_and_an_admin(nest, tmp_path):
    hub, http = nest
    make_worker(tmp_path, http.url, "laptop").join(http.url, OPERATOR)
    _, r = http.post({"op": "workers", "name": "laptop", "args": {"action": "share"}})
    assert r["error"]["code"] == "bad_request"                  # its owner never offered it
    w = make_worker(tmp_path, http.url, "gpu-box", shared=True)
    assert w.join(http.url, OPERATOR)["shared"] == "asked"
    assert not hub.workers.allows("gpu-box", "oth")
    _, r = http.post({"op": "workers", "name": "gpu-box", "args": {"action": "share"}}, token=OPERATOR)
    assert r["error"]["code"] == "forbidden"
    _, r = http.post({"op": "workers", "name": "gpu-box", "args": {"action": "share"}})
    assert r["value"]["shared"] == "yes"
    w.hello()
    assert hub.workers.allows("gpu-box", "oth")
    _, r = http.post({"op": "workers", "name": "gpu-box", "args": {"action": "unshare"}}, token=OTHER)
    assert r["error"]["code"] == "forbidden"
    _, r = http.post({"op": "workers", "name": "gpu-box", "args": {"action": "unshare"}}, token=OPERATOR)
    assert r["value"]["shared"] == "no" and not hub.workers.allows("gpu-box", "oth")


def test_a_shared_worker_takes_only_the_people_its_owner_allows(nest, tmp_path):
    hub, http = nest
    w = make_worker(tmp_path, http.url, shared=True, people=["ad*"])
    w.join(http.url, OPERATOR)
    http.post({"op": "workers", "name": "gpu-box", "args": {"action": "share"}})
    w.hello()
    assert hub.workers.allows("gpu-box", "ada") and not hub.workers.allows("gpu-box", "oth")


def test_workers_report_and_only_owners_and_admins_see_private_details(nest, tmp_path):
    hub, http = nest
    w = make_worker(tmp_path, http.url)
    w.join(http.url, OPERATOR)
    w.hello()
    _, r = http.post({"op": "workers"}, token=VIEWER)
    seen = r["value"][0]
    assert seen["online"] and seen["cpus"] >= 1 and seen["owner"] == "olu"
    assert not {"host", "addresses", "platform", "link"} & set(seen)
    for token in (ADMIN, OPERATOR):
        _, r = http.post({"op": "workers"}, token=token)
        assert "host" in r["value"][0] and "addresses" in r["value"][0]
    _, r = http.post({"op": "who"}, token=VIEWER)
    assert r["value"]["workers"][0]["worker"] == "gpu-box"


def test_requests_are_signed_and_cannot_be_replayed(nest, tmp_path):
    hub, http = nest
    w = make_worker(tmp_path, http.url)
    w.join(http.url, OPERATOR)
    body = json.dumps({"action": "hello"}).encode()
    header, _ = signing.request_header(w.name, w.key, body)

    def send(header, body=body):
        req = urllib.request.Request(http.url + "/api/v1/worker", data=body, method="POST", headers={"Authorization": header})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status
        except urllib.error.HTTPError as e:
            return e.code
    assert send(header) == 200
    assert send(header) == 401                                    # the same signed request, again
    forged, _ = signing.request_header(w.name, "x" * 43, body)
    assert send(forged) == 401
    tampered, _ = signing.request_header(w.name, w.key, body)
    assert send(tampered, json.dumps({"action": "next"}).encode()) == 401
    assert send(f"Bearer {OPERATOR}") == 401                     # a person's token is not a worker's


def test_a_worker_trusts_only_replies_signed_with_its_key(nest, tmp_path):
    hub, http = nest
    w = make_worker(tmp_path, http.url)
    w.join(http.url, OPERATOR)
    hub.workers.entries["gpu-box"]["key"] = "y" * 43               # as if someone else answered
    with pytest.raises(LabratError) as e:
        w.hello()
    assert e.value.code == "unauthorised"


def test_a_removed_worker_is_refused(nest, tmp_path):
    hub, http = nest
    w = make_worker(tmp_path, http.url)
    w.join(http.url, OPERATOR)
    _, r = http.post({"op": "workers", "name": "gpu-box", "args": {"action": "remove"}}, token=OPERATOR)
    assert r["value"]["removed"]
    with pytest.raises(LabratError) as e:
        w.hello()
    assert e.value.code == "unauthorised"


def test_plain_http_only_to_a_private_nest():
    def ip(*parts):                                              # written out here would look like a real address
        return ".".join(map(str, parts))
    for url in ("http://127.0.0.1:8780", f"http://{ip(192, 168, 1, 5)}:8780", f"http://{ip(100, 101, 102, 103)}:8780",
                "http://nest.local:8780", "https://nest.example.org"):
        check_url(url)
    public = f"http://{ip(8, 8, 8, 8)}:8780"
    with pytest.raises(LabratError):
        check_url(public)
    check_url(public, allow_http=True)


def test_owner_settings_are_checked():
    with pytest.raises(LabratError):
        check_config({"acept": "any"})
    with pytest.raises(LabratError):
        check_config({"accept": "scripts"})
    with pytest.raises(LabratError):
        check_config({"hours": "late"})
    assert in_hours("")
    import time
    noon = time.mktime((2026, 11, 1, 12, 0, 0, 0, 0, -1))
    assert in_hours("09:00-17:00", noon) and not in_hours("18:00-07:00", noon)
    assert in_hours("18:00-07:00", noon + 12 * 3600)
