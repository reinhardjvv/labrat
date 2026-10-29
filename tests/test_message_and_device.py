import pytest

from labrat.device import Attr, convert
from labrat.drivers import build
from labrat.message import LabratError, parse, role_allows


def test_a_message_is_checked_before_anything_runs():
    assert parse({"op": "set", "device": "psu", "name": "VoltageLevel", "value": 1}).device == "psu"
    for bad in ([], {"op": "fly"}, {"op": "set", "device": "psu", "name": "x"}, {"op": "get", "device": "psu", "name": "x", "extra": 1}):
        with pytest.raises(LabratError) as e:
            parse(bad)
        assert e.value.code == "bad_request"


def test_roles_grow_from_viewer_to_admin_and_anyone_may_stop():
    assert role_allows("viewer", "get") and not role_allows("viewer", "set")
    assert role_allows("operator", "set") and not role_allows("operator", "adopt")
    assert role_allows("admin", "adopt") and role_allows("viewer", "stop")
    assert not role_allows("nobody", "get")


def test_limits_only_narrow():
    a = Attr("VoltageLevel", min=0, max=30, unit="V")
    a.narrow(hi=12)
    a.narrow(hi=50)                                  # looser: ignored
    assert a.max == 12
    with pytest.raises(LabratError) as e:
        a.check(13)
    assert e.value.code == "limit" and "12 V" in e.value.message


def test_values_must_be_what_they_say():
    assert convert("bool", "on") is True and convert("int", 3.0) == 3
    for kind, v in (("bool", 2), ("float", "nan"), ("float", True), ("int", 2.5)):
        with pytest.raises(LabratError):
            convert(kind, v)


def test_a_device_refuses_out_of_range_and_goes_safe():
    psu = build("psu", {"driver": "sim-supply", "limits": {"VoltageLevel": {"max": 5}}})
    psu.set("VoltageLevel", 4, "1")
    psu.set("OutputEnabled", True, "1")
    with pytest.raises(LabratError):
        psu.set("VoltageLevel", 6, "1")
    with pytest.raises(LabratError):
        psu.set("VoltageLevel", 1)                    # two channels: which one?
    assert psu.call("Measure", {"type": "voltage"}, "1")["value"] == pytest.approx(4)
    assert psu.make_safe() == []
    assert psu.get("OutputEnabled", "1") is False and psu.get("OutputEnabled", "2") is False


def test_an_unknown_driver_or_setting_is_named():
    with pytest.raises(LabratError, match="unknown driver"):
        build("x", {"driver": "nope"})
    with pytest.raises(LabratError, match="no attribute"):
        build("x", {"driver": "sim-supply", "safe": {"Nope": 1}})
