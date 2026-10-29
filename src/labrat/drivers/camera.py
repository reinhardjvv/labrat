"""Cameras through ffmpeg: USB and CSI cameras (Video4Linux2), and PoE or network cameras (RTSP, ONVIF, MJPEG).

    [devices.cam]
    driver = "camera"
    source = "/dev/video0"              # or rtsp://user:password@192.0.2.30:554/stream1, or http://.../video.mjpg
    width = 1280                        # for /dev/video* only; a network camera sends its own size
    height = 720
    fps = 15

A password in the source stays in the configuration file: LABRAT never shows the source to clients.
Machine-vision cameras (GigE Vision, USB3 Vision) use the genicam driver.

One ffmpeg process reads the camera while anyone is watching, and every viewer gets the same frames. It stops
when the last viewer leaves. Snapshot returns one JPEG frame as base64, for clients that want a picture
inside a JSON reply. Needs ffmpeg (apt install ffmpeg) and, for local cameras, membership of the video group.
"""
from __future__ import annotations

import base64
import shutil
import subprocess
import threading
import time

from ..device import Attr, Device, Function, Stream
from ..message import LabratError

BOUNDARY = "labratframe"


class Camera(Device):
    kind = "camera"

    def __init__(self, name: str, config: dict | None = None):
        super().__init__(name, config)
        self.video = str(self.config.get("source", "/dev/video0"))
        self.network = self.video.split("://")[0] in ("rtsp", "rtsps", "http", "https")
        self.width, self.height = int(self.config.get("width", 1280)), int(self.config.get("height", 720))
        self.fps = float(self.config.get("fps", 15))
        self.attrs = {
            "Width": Attr("Width", "int", "px", access="r"),
            "Height": Attr("Height", "int", "px", access="r"),
            "FrameRate": Attr("FrameRate", "float", "Hz", access="r"),
        }
        self.functions = {"Snapshot": Function("Snapshot", "one JPEG frame, base64 encoded")}
        self.streams = {"Video": Stream("Video", f"multipart/x-mixed-replace; boundary={BOUNDARY}", "live MJPEG")}
        self.frame: bytes | None = None
        self.frame_no = 0
        self.viewers = 0
        self.cond = threading.Condition()
        self.proc: subprocess.Popen | None = None

    def read(self, name, channel):
        return {"Width": self.width, "Height": self.height, "FrameRate": self.fps}[name]

    # -- the shared reader ----------------------------------------------------------------------------------
    def _start(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            return
        exe = shutil.which("ffmpeg")
        if exe is None:
            raise LabratError("unavailable", "the camera driver needs ffmpeg (apt install ffmpeg)")
        if self.video.startswith("rtsp"):
            src = ["-rtsp_transport", "tcp", "-i", self.video]
        elif self.network:
            src = ["-i", self.video]
        else:
            src = ["-f", "v4l2", "-framerate", f"{self.fps:g}", "-video_size", f"{self.width}x{self.height}", "-i", self.video]
        cmd = [exe, "-loglevel", "error", *src, "-f", "mjpeg", "-q:v", "5", "-"]
        self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        threading.Thread(target=self._pump, args=(self.proc,), name=f"labrat-camera-{self.name}", daemon=True).start()

    def _pump(self, proc: subprocess.Popen) -> None:
        buf = b""
        while True:
            chunk = proc.stdout.read(65536)
            if not chunk:
                break
            buf += chunk
            while True:
                start = buf.find(b"\xff\xd8")
                end = buf.find(b"\xff\xd9", start + 2)
                if start < 0 or end < 0:
                    break
                with self.cond:
                    self.frame, self.frame_no = buf[start:end + 2], self.frame_no + 1
                    self.cond.notify_all()
                buf = buf[end + 2:]
            if len(buf) > 8 << 20:                          # no frame boundary in 8 MB: start over
                buf = b""
        with self.cond:
            self.cond.notify_all()

    def _stop_if_idle(self) -> None:
        with self.cond:
            if self.viewers == 0 and self.proc is not None:
                self.proc.terminate()
                self.proc = None

    def _next(self, after: int, timeout: float = 5.0) -> tuple[int, bytes]:
        deadline = time.monotonic() + timeout
        with self.cond:
            while self.frame_no <= after:
                left = deadline - time.monotonic()
                if left <= 0 or (self.proc is not None and self.proc.poll() is not None):
                    raise LabratError("device", f"{self.name}: no frame from the camera")
                self.cond.wait(left)
            return self.frame_no, self.frame

    # -- what clients see -----------------------------------------------------------------------------------
    def run(self, name, args, channel):
        with self.cond:
            self.viewers += 1
            self._start()
            last = self.frame_no
        try:
            _, frame = self._next(last)
        finally:
            with self.cond:
                self.viewers -= 1
            self._stop_if_idle()
        return {"content_type": "image/jpeg", "base64": base64.b64encode(frame).decode()}

    def stream(self, name, channel):
        with self.cond:
            self.viewers += 1
            self._start()
            last = self.frame_no
        try:
            while True:
                last, frame = self._next(last)
                yield (f"--{BOUNDARY}\r\nContent-Type: image/jpeg\r\nContent-Length: {len(frame)}\r\n\r\n").encode() + frame + b"\r\n"
        finally:
            with self.cond:
                self.viewers -= 1
            self._stop_if_idle()

    def close(self) -> None:
        if self.proc is not None:
            self.proc.terminate()
