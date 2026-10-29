"""The HTTP face of a nest or rat: health, one message endpoint, and two read-only conveniences.

    GET  /api/v1/health              who this is, no token needed
    POST /api/v1/message             one message, or a list of up to 100 (answered in order)
    GET  /api/v1/devices             the same as {"op": "list"}
    GET  /api/v1/devices/<device>    the same as {"op": "describe", "device": "<device>"}
    GET  /api/v1/streams/<device>/<stream>[?channel=<ch>]
                                     a device's stream (camera video, live readings), sent as it comes

Every route but health needs 'Authorization: Bearer <token>'.
"""
from __future__ import annotations

import itertools
import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

from .hub import Hub
from .message import LabratError, fail

MAX_BODY = 32 << 20                  # room for a full-HD frame in base64
MAX_BATCH = 100
MAX_STREAMS = 16
log = logging.getLogger("labrat")


def make_handler(hub: Hub):
    streams = threading.BoundedSemaphore(MAX_STREAMS)

    class Handler(BaseHTTPRequestHandler):
        server_version = "labrat"
        sys_version = ""
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            log.debug("%s %s", self.address_string(), fmt % args)

        timeout = 60                                    # an idle or half-sent request does not hold a thread for ever

        def _send(self, status: int, body) -> None:
            data = json.dumps(body, default=str).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            if status == 401:
                self.send_header("WWW-Authenticate", "Bearer")
            if status >= 400 and self.command == "POST":
                self.send_header("Connection", "close")     # the body may be unread: do not parse it as a request
                self.close_connection = True
            self.end_headers()
            self.wfile.write(data)

        def _one(self, obj, who) -> None:
            reply = hub.handle(obj, who)
            status = 200 if reply.get("ok") else LabratError(reply["error"]["code"], "").status
            self._send(status, reply)

        def do_GET(self):
            path = self.path.split("?")[0].rstrip("/")
            if path == "/api/v1/health":
                challenge = (parse_qs(urlsplit(self.path).query).get("challenge") or [None])[-1]
                return self._send(200, {"ok": True, "value": hub.health(challenge[:128] if challenge else None)})
            try:
                who = hub.authenticate(self.headers.get("Authorization"))
            except LabratError as e:
                return self._send(e.status, fail(e))
            if path == "/api/v1/devices":
                return self._one({"op": "list"}, who)
            if path.startswith("/api/v1/devices/"):
                return self._one({"op": "describe", "device": unquote(path[len("/api/v1/devices/"):])}, who)
            if path.startswith("/api/v1/streams/") and path.count("/") >= 5:
                return self._stream(path[len("/api/v1/streams/"):], who)
            self._send(404, fail(LabratError("not_found", f"no route {path}")))

        def _stream(self, rest: str, who) -> None:
            device, _, name = rest.rpartition("/")
            channel = (parse_qs(urlsplit(self.path).query).get("channel") or [None])[-1]
            if not streams.acquire(blocking=False):
                return self._send(503, fail(LabratError("unavailable", f"already {MAX_STREAMS} streams open")))
            try:
                try:
                    content_type, chunks = hub.open_stream(unquote(device), unquote(name), channel, who)
                    first = next(chunks, b"")                # errors such as 'already streaming' come before the headers
                except LabratError as e:
                    return self._send(e.status, fail(e))
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Transfer-Encoding", "chunked")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                try:
                    for chunk in itertools.chain([first], chunks):
                        if chunk:
                            self.wfile.write(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
                            self.wfile.flush()
                    self.wfile.write(b"0\r\n\r\n")
                except (BrokenPipeError, ConnectionResetError):
                    pass                                     # the viewer left
                except LabratError as e:
                    log.warning("stream %s ended: %s", rest, e.message)
                finally:
                    close = getattr(chunks, "close", None)
                    if close:
                        close()
                self.close_connection = True
            finally:
                streams.release()

        def do_POST(self):
            path = self.path.split("?")[0].rstrip("/")
            try:
                who = hub.authenticate(self.headers.get("Authorization"))
                if path != "/api/v1/message":
                    raise LabratError("not_found", f"no route {path}")
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                except ValueError:
                    raise LabratError("bad_request", "Content-Length is not a number") from None
                if not 0 < length <= MAX_BODY:
                    raise LabratError("bad_request", f"send a JSON body of at most {MAX_BODY} bytes")
                try:
                    obj = json.loads(self.rfile.read(length))
                except ValueError:
                    raise LabratError("bad_request", "the body is not valid JSON") from None
            except LabratError as e:
                return self._send(e.status, fail(e))
            if isinstance(obj, list):
                if not 0 < len(obj) <= MAX_BATCH:
                    return self._send(400, fail(LabratError("bad_request", f"a batch holds 1 to {MAX_BATCH} messages")))
                return self._send(200, [hub.handle(o, who) for o in obj])
            self._one(obj, who)

    return Handler


class Server:
    """A running HTTP server for a hub, in a background thread."""

    def __init__(self, hub: Hub, host: str, port: int):
        self.hub = hub
        self.httpd = ThreadingHTTPServer((host, port), make_handler(hub))
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, name="labrat-http", daemon=True)

    def start(self) -> Server:
        self.thread.start()
        return self

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
