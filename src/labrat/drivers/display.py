"""A screen used as an instrument: an HDMI spatial light modulator (SLM), a DMD or a projector.

    [devices.slm]
    driver = "display"
    framebuffer = "/dev/fb0"        # the HDMI output the SLM is on; a Raspberry Pi 4 or 5 has fb0 and fb1

The driver writes frames straight into the Linux framebuffer, so no desktop is needed (use a Lite or server
image and give the rat's user the video group). Size and pixel format come from /sys/class/graphics; width,
height and bits_per_pixel in the configuration override them.

ShowImage takes an 8-bit grey frame as base64, width x height bytes row by row, or a PNG when Pillow is
installed. ShowPattern draws blank, grating (binary or blazed, vertical or horizontal) and checkerboard.
The safe state is a blank screen at level 0.
"""
from __future__ import annotations

import base64
import io
import os
from pathlib import Path

from ..device import Attr, Device, Function
from ..message import LabratError


def _sysfs(fb: str, name: str) -> str | None:
    p = Path("/sys/class/graphics") / Path(fb).name / name
    return p.read_text().strip() if p.is_file() else None


class Display(Device):
    kind = "display"

    def __init__(self, name: str, config: dict | None = None):
        super().__init__(name, config)
        self.fb = str(self.config.get("framebuffer", "/dev/fb0"))
        size = (_sysfs(self.fb, "virtual_size") or "0,0").split(",")
        self.width = int(self.config.get("width") or size[0])
        self.height = int(self.config.get("height") or size[1])
        self.bpp = int(self.config.get("bits_per_pixel") or _sysfs(self.fb, "bits_per_pixel") or 32)
        self.stride = int(self.config.get("stride") or _sysfs(self.fb, "stride") or self.width * self.bpp // 8)
        if self.width <= 0 or self.height <= 0:
            raise LabratError("unavailable", f"{name}: cannot read the size of {self.fb}; give width and height")
        if self.bpp not in (16, 24, 32):
            raise LabratError("unavailable", f"{name}: {self.bpp} bits per pixel is not supported (16, 24 or 32)")
        self.showing = "nothing yet"
        self.attrs = {
            "Width": Attr("Width", "int", "px", access="r"),
            "Height": Attr("Height", "int", "px", access="r"),
            "Showing": Attr("Showing", "str", access="r", doc="what is on the screen"),
            "Blank": Attr("Blank", "bool", doc="true shows level 0 everywhere"),
        }
        self.functions = {
            "ShowImage": Function("ShowImage", "an 8-bit grey frame (base64, width x height bytes) or a PNG (base64)",
                                  {"data": "str", "format": "str"}),
            "ShowPattern": Function("ShowPattern", "blank, grating or checkerboard",
                                    {"pattern": "str", "period": "int", "low": "int", "high": "int",
                                     "direction": "str", "blazed": "bool", "level": "int"}),
        }
        self.safe = {"Blank": True}

    def read(self, name, channel):
        return {"Width": self.width, "Height": self.height, "Showing": self.showing,
                "Blank": self.showing == "blank at 0"}[name]

    def write(self, name, value, channel):
        if value:
            self._show(bytes(self.width * self.height), "blank at 0")

    def run(self, name, args, channel):
        if name == "ShowImage":
            self._show(self._decode(args.get("data", ""), args.get("format", "gray8")), "image")
        else:
            gray, what = self._pattern(args)
            self._show(gray, what)
        return {"showing": self.showing, "width": self.width, "height": self.height}

    # -- frames ---------------------------------------------------------------------------------------------
    def _decode(self, data: str, fmt: str) -> bytes:
        try:
            raw = base64.b64decode(data, validate=True)
        except ValueError:
            raise LabratError("bad_request", "data is not base64") from None
        if fmt == "png":
            try:
                from PIL import Image
            except ImportError:
                raise LabratError("unavailable", "PNG frames need Pillow (pip install pillow); or send format gray8") from None
            img = Image.open(io.BytesIO(raw)).convert("L")
            if img.size != (self.width, self.height):
                img = img.resize((self.width, self.height))
            return img.tobytes()
        if fmt != "gray8":
            raise LabratError("bad_request", "format is gray8 or png")
        if len(raw) != self.width * self.height:
            raise LabratError("bad_request", f"a gray8 frame is {self.width} x {self.height} = {self.width * self.height} bytes, not {len(raw)}")
        return raw

    def _pattern(self, a: dict) -> tuple[bytes, str]:
        w, h = self.width, self.height
        kind = a.get("pattern", "blank")
        low, high = int(a.get("low", 0)), int(a.get("high", 255))
        if not (0 <= low <= 255 and 0 <= high <= 255):
            raise LabratError("bad_request", "low and high are grey levels from 0 to 255")
        if kind == "blank":
            level = int(a.get("level", 0))
            if not 0 <= level <= 255:
                raise LabratError("bad_request", "level is from 0 to 255")
            return bytes([level]) * (w * h), f"blank at {level}"
        period = int(a.get("period", 16))
        if period < 2:
            raise LabratError("bad_request", "period is at least 2 pixels")
        if kind == "grating":
            blazed, vertical = bool(a.get("blazed", False)), a.get("direction", "vertical") == "vertical"
            n = w if vertical else h
            line = bytes(low + (high - low) * (i % period) // (period - 1) if blazed else (high if (i % period) < period // 2 else low)
                         for i in range(n))
            gray = line * h if vertical else b"".join(bytes([v]) * w for v in line)
            return gray, f"{'blazed' if blazed else 'binary'} {a.get('direction', 'vertical')} grating, period {period} px"
        if kind == "checkerboard":
            rows = [bytes(high if ((x // period) + (y // period)) % 2 else low for x in range(w)) for y in range(2 * period)]
            return b"".join(rows[y % (2 * period)] for y in range(h)), f"checkerboard, {period} px squares"
        raise LabratError("bad_request", "pattern is blank, grating or checkerboard")

    def _show(self, gray: bytes, what: str) -> None:
        n = self.width * self.height
        if self.bpp == 32:
            px = bytearray(n * 4)
            px[0::4] = px[1::4] = px[2::4] = gray
            px[3::4] = b"\xff" * n
        elif self.bpp == 24:
            px = bytearray(n * 3)
            px[0::3] = px[1::3] = px[2::3] = gray
        else:                                                 # RGB565, little endian
            lo = bytes(((g >> 2) << 5 & 0xE0) | (g >> 3) for g in range(256))
            hi = bytes((g >> 3) << 3 | (g >> 5) for g in range(256))
            px = bytearray(n * 2)
            px[0::2], px[1::2] = gray.translate(lo), gray.translate(hi)
        row = self.width * self.bpp // 8
        try:
            fd = os.open(self.fb, os.O_WRONLY)
        except OSError as e:
            raise LabratError("unavailable", f"{self.name}: cannot open {self.fb}: {e}") from None
        try:
            if self.stride == row:
                os.pwrite(fd, px, 0)
            else:
                for y in range(self.height):
                    os.pwrite(fd, px[y * row:(y + 1) * row], y * self.stride)
        finally:
            os.close(fd)
        self.showing = what
