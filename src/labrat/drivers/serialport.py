"""Serial devices with their own text commands: pumps, stages, controllers, Arduinos, anything line-based.

    [devices.pump]
    driver = "serial"
    port = "/dev/ttyUSB0"                  # or a pyserial URL: socket://192.0.2.40:4001, rfc2217://...
    baud = 9600
    parity = "none"                        # none, even, odd
    line_ending = "crlf"                   # what the device expects after a command: lf, cr or crlf
    safe_commands = ["STOP"]

    [devices.pump.attributes.Rate]
    type = "float"
    unit = "ml/min"
    min = 0
    max = 50
    get = "RAT?"
    reply = "RAT=([-0-9.]+)"               # the part of the reply that holds the value
    set = "RAT {value}"

    [devices.pump.attributes.Running]
    type = "bool"
    get = "STATUS"
    reply = "RUN=(\\w+)"
    on = "YES"
    off = "NO"
    set = "{value}"                        # sends the on or off word

Attributes, functions, raw commands and safe commands work as in the scpi driver. The difference is the port
settings, the line ending and that the device is not asked *IDN? unless identify = true. Needs pyserial
(pip install 'labrat-lab[serial]').
"""
from __future__ import annotations

from ..message import LabratError
from .scpi import Scpi, SerialTransport

EOL = {"lf": b"\n", "cr": b"\r", "crlf": b"\r\n"}
PARITY = {"none": "N", "even": "E", "odd": "O", "mark": "M", "space": "S"}


class Serial(Scpi):
    kind = "instrument"

    def connect(self) -> SerialTransport:
        c = self.config
        if not c.get("port"):
            raise LabratError("bad_request", f"{self.name}: a serial device needs a port, such as /dev/ttyUSB0")
        c.setdefault("identify", False)
        eol = str(c.get("line_ending", "lf"))
        read_eol = str(c.get("reply_ending", eol))
        if eol not in EOL or read_eol not in EOL:
            raise LabratError("bad_request", f"{self.name}: line_ending and reply_ending are lf, cr or crlf")
        if str(c.get("parity", "none")) not in PARITY:
            raise LabratError("bad_request", f"{self.name}: parity is one of {', '.join(PARITY)}")
        settings = {"baudrate": int(c.get("baud", 9600)), "bytesize": int(c.get("data_bits", 8)),
                    "parity": PARITY[str(c.get("parity", "none"))], "stopbits": float(c.get("stop_bits", 1))}
        if settings["stopbits"].is_integer():
            settings["stopbits"] = int(settings["stopbits"])
        return SerialTransport(f"serial://{c['port']}", float(c.get("timeout", 2.0)), str(c["port"]), settings,
                               EOL[eol], EOL[read_eol][-1:])
