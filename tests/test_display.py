import base64

import pytest

from labrat.drivers import build
from labrat.message import LabratError


@pytest.fixture
def slm(tmp_path):
    fb = tmp_path / "fb"
    fb.write_bytes(bytes(8 * 4 * 4))
    return fb, build("slm", {"driver": "display", "framebuffer": str(fb), "width": 8, "height": 4, "bits_per_pixel": 32})


def gray(fb):
    return fb.read_bytes()[0::4]


def test_an_image_lands_in_the_framebuffer(slm):
    fb, dev = slm
    frame = bytes(range(32))
    dev.call("ShowImage", {"data": base64.b64encode(frame).decode()})
    assert gray(fb) == frame and fb.read_bytes()[3::4] == b"\xff" * 32
    with pytest.raises(LabratError, match="32 bytes"):
        dev.call("ShowImage", {"data": base64.b64encode(b"x").decode()})


def test_patterns(slm):
    fb, dev = slm
    dev.call("ShowPattern", {"pattern": "grating", "period": 4})
    assert gray(fb)[:8] == bytes([255, 255, 0, 0] * 2)
    dev.call("ShowPattern", {"pattern": "grating", "period": 4, "blazed": True, "high": 3})
    assert gray(fb)[:4] == bytes([0, 1, 2, 3])
    dev.call("ShowPattern", {"pattern": "checkerboard", "period": 2})
    assert gray(fb)[:4] == bytes([0, 0, 255, 255]) and gray(fb)[16:20] == bytes([255, 255, 0, 0])


def test_the_safe_state_is_blank(slm):
    fb, dev = slm
    dev.call("ShowPattern", {"pattern": "blank", "level": 200})
    assert dev.make_safe() == []
    assert gray(fb) == bytes(32) and dev.get("Blank") is True
