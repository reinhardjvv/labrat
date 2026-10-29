"""Machine-vision cameras on GigE Vision (PoE) and USB3 Vision through Aravis: Allied Vision, Basler, FLIR and others.

    [devices.cam]
    driver = "genicam"
    camera = "Allied Vision-xxxxxxxx"    # an id from 'arv-tool-0.8'; leave it out for the first camera found
    pixel_format = "Mono8"
    [devices.cam.limits.ExposureTime]
    max = 0.05                          # seconds; tighter than the camera's own range

Exposure and gain come with the camera's own ranges, which the configuration may narrow. Snapshot returns one
frame as base64 (PGM, or JPEG when Pillow is installed). The Frames stream sends frames as they arrive.
Needs Aravis 0.8 with its Python bindings (Debian, Ubuntu and Raspberry Pi OS: apt install gir1.2-aravis-0.8
python3-gi), and on GigE a network interface with jumbo frames for full frame rates.
"""
from __future__ import annotations

import base64
import io
import threading

from ..device import Attr, Device, Function, Stream
from ..message import LabratError

BOUNDARY = "labratframe"


class Genicam(Device):
    kind = "camera"

    def __init__(self, name: str, config: dict | None = None):
        super().__init__(name, config)
        try:
            import gi
            gi.require_version("Aravis", "0.8")
            from gi.repository import Aravis
        except (ImportError, ValueError):
            raise LabratError("unavailable", "the genicam driver needs Aravis 0.8: apt install gir1.2-aravis-0.8 python3-gi") from None
        self.ar = Aravis
        try:
            self.cam = Aravis.Camera.new(self.config.get("camera"))
        except Exception as e:
            raise LabratError("unavailable", f"{name}: no camera {self.config.get('camera') or ''}: {e}") from None
        fmt = str(self.config.get("pixel_format", "Mono8"))
        try:
            self.cam.set_pixel_format_from_string(fmt)
        except Exception as e:
            raise LabratError("bad_request", f"{name}: pixel format {fmt}: {e}") from None
        self.mono = fmt == "Mono8"
        emin, emax = self.cam.get_exposure_time_bounds()
        gmin, gmax = self.cam.get_gain_bounds()
        self.attrs = {
            "ExposureTime": Attr("ExposureTime", "float", "s", min=emin / 1e6, max=emax / 1e6),
            "Gain": Attr("Gain", "float", "dB", min=gmin, max=gmax),
            "Width": Attr("Width", "int", "px", access="r"),
            "Height": Attr("Height", "int", "px", access="r"),
            "Model": Attr("Model", "str", access="r"),
        }
        self.functions = {"Snapshot": Function("Snapshot", "one frame, base64 (PGM, or JPEG with Pillow)")}
        self.streams = {"Frames": Stream("Frames", f"multipart/x-mixed-replace; boundary={BOUNDARY}", "frames as they arrive")}
        self._grab = threading.Lock()

    def read(self, name, channel):
        if name == "ExposureTime":
            return self.cam.get_exposure_time() / 1e6
        if name == "Gain":
            return self.cam.get_gain()
        if name in ("Width", "Height"):
            _, _, w, h = self.cam.get_region()
            return w if name == "Width" else h
        return f"{self.cam.get_vendor_name()} {self.cam.get_model_name()}"

    def write(self, name, value, channel):
        if name == "ExposureTime":
            self.cam.set_exposure_time(value * 1e6)
        else:
            self.cam.set_gain(value)

    def _encode(self, data: bytes, w: int, h: int) -> tuple[str, bytes]:
        try:
            from PIL import Image
            out = io.BytesIO()
            Image.frombytes("L", (w, h), data).save(out, "JPEG", quality=90)
            return "image/jpeg", out.getvalue()
        except ImportError:
            return "image/x-portable-graymap", f"P5 {w} {h} 255\n".encode() + data

    def _frame(self, buf) -> tuple[str, bytes]:
        if buf is None or buf.get_status() != self.ar.BufferStatus.SUCCESS:
            raise LabratError("device", f"{self.name}: no frame")
        w, h = buf.get_image_width(), buf.get_image_height()
        data = bytes(buf.get_data())
        if not self.mono:
            return "application/octet-stream", data
        return self._encode(data[:w * h], w, h)

    def run(self, name, args, channel):
        with self._grab:
            kind, data = self._frame(self.cam.acquisition(3_000_000))
        return {"content_type": kind, "base64": base64.b64encode(data).decode()}

    def stream(self, name, channel):
        if not self._grab.acquire(blocking=False):
            raise LabratError("locked", f"{self.name} is already streaming")
        stream = None
        try:
            stream = self.cam.create_stream(None, None)
            for _ in range(4):
                stream.push_buffer(self.ar.Buffer.new_allocate(self.cam.get_payload()))
            self.cam.set_acquisition_mode(self.ar.AcquisitionMode.CONTINUOUS)
            self.cam.start_acquisition()
            while True:
                buf = stream.timeout_pop_buffer(3_000_000)
                kind, data = self._frame(buf)
                stream.push_buffer(buf)
                yield f"--{BOUNDARY}\r\nContent-Type: {kind}\r\nContent-Length: {len(data)}\r\n\r\n".encode() + data + b"\r\n"
        finally:
            try:
                self.cam.stop_acquisition()
            finally:
                self._grab.release()
