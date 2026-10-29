# Drivers

A driver turns a piece of equipment into a LABRAT device. `labrat drivers` lists the drivers an installation
has. Settings go in the device's table in `nest.toml` or `rat.toml`, next to `driver`, `limits` and `safe`
([configuration.md](configuration.md)).

## scpi

Any instrument that takes text commands, over TCP, serial or USBTMC. The attributes and functions are written
in the configuration, so a new instrument needs no code.

```toml
[devices.psu]
driver = "scpi"
address = "tcp://192.0.2.10:5025"      # serial:///dev/ttyUSB0?baud=9600 or usbtmc:///dev/usbtmc0
timeout = 2.0
safe_commands = ["OUTP OFF"]

[devices.psu.attributes.VoltageLevel]
type = "float"
unit = "V"
min = 0
max = 30
get = "VOLT?"
set = "VOLT {value}"

[devices.psu.attributes.OutputEnabled]
type = "bool"
set = "OUTP {value}"
on = "ON"
off = "OFF"

[devices.psu.functions.Identify]
query = "*IDN?"
```

Commands may use `{value}` and, for attributes with `channels = ["1", "2"]`, `{channel}`. Values containing a
newline or `;` are refused, so a value cannot carry a second command. Raw commands are off. `raw = "query"`
adds a `Query` function that sends only commands ending in `?`, and `raw = "on"` adds `Write` as well. The
limits cannot check either, and some queries change an instrument (`READ?` can switch an output on), so
prefer functions in the configuration. The audit log records the commands sent. Serial needs `labrat-lab[serial]`.

## gpio

Linux GPIO lines through the character device, with libgpiod 2 (`labrat-lab[gpio]`).

```toml
[devices.relays]
driver = "gpio"
chip = "/dev/gpiochip0"                # gpiochip4 on a Raspberry Pi 5 with older kernels, gpioinfo lists them
outputs = { heater = 17, lamp = 27 }   # name = line offset
inputs = { door = 22 }
safe = { heater = false }
```

Outputs start inactive. Each line is a boolean attribute named as in the configuration.

## camera

USB and CSI cameras, and network cameras, through ffmpeg (`apt install ffmpeg`).

```toml
[devices.cam]
driver = "camera"
source = "/dev/video0"                 # or rtsp://user:password@192.0.2.30:554/stream1, or an MJPEG URL
width = 1280
height = 720
fps = 15
```

The `Video` stream is MJPEG, shared by every viewer, and the camera runs only while someone watches.
`Snapshot` returns one JPEG frame as base64. Most PoE security and lab cameras, ONVIF ones included, give an
RTSP address that works with this driver. LABRAT never shows the source to clients, so a password in it stays in the configuration.

## genicam

Machine-vision cameras on GigE Vision and USB3 Vision through [Aravis](https://github.com/AravisProject/aravis):
Allied Vision (Alvium, Mako, Manta, Prosilica), Basler, FLIR and others.

```toml
[devices.cam]
driver = "genicam"
camera = "Allied Vision-xxxxxxxx"      # arv-tool-0.8 lists the ids, leave it out for the first camera
pixel_format = "Mono8"
```

`ExposureTime` (s) and `Gain` (dB) carry the camera's own ranges, which `limits` may narrow. `Snapshot`
returns one frame and the `Frames` stream sends frames as they arrive, as JPEG with Pillow installed and as
PGM without it. Install Aravis with `apt install gir1.2-aravis-0.8 python3-gi` and create the virtual
environment with `--system-site-packages`. GigE cameras reach their full frame rate with jumbo frames on the
network interface.

## display

A screen used as an instrument: HDMI spatial light modulators (SLMs), DMDs and projectors. The driver writes
frames straight into the Linux framebuffer, so no desktop is needed.

```toml
[devices.slm]
driver = "display"
framebuffer = "/dev/fb0"               # the HDMI output, a Raspberry Pi 4 or 5 has fb0 and fb1
```

`ShowImage` takes an 8-bit grey frame as base64, width by height bytes row by row, or a PNG with
`format = "png"` and Pillow installed. `ShowPattern` draws `blank` (at `level`), `grating` (`period`, `low`,
`high`, `direction`, `blazed`) and `checkerboard` (`period`). The safe state is a blank screen at level 0. The
rat's user needs the `video` group, and the console should not blank the screen (`consoleblank=0` on the
kernel command line).

## sim-supply and sim-gpio

Simulated devices. `sim-supply` is a two-channel supply with a resistive load (`channels`, `max_voltage`,
`max_current`, `load_ohm`) and a `Readings` stream of newline-delimited JSON. `sim-gpio` takes the same
`outputs` and `inputs` as `gpio`.

## Writing a driver

A driver is a subclass of `labrat.device.Device`. It fills `attrs`, `functions`, `streams` and `safe` in its
constructor and implements `read`, `write`, `run` and, for streams, `stream`. The base class does the type
conversion, the limit checks, channels, the safe state and the description.

```python
from labrat.device import Attr, Device, Function


class AcmeHeater(Device):
    kind = "temperature-controller"

    def __init__(self, name, config=None):
        super().__init__(name, config)
        self.link = open_link(self.config["address"])          # your transport
        self.attrs = {
            "Setpoint": Attr("Setpoint", "float", "degC", min=0, max=150),
            "Temperature": Attr("Temperature", "float", "degC", access="r"),
            "Enabled": Attr("Enabled", "bool"),
        }
        self.functions = {"Autotune": Function("Autotune", "run the PID autotune")}
        self.safe = {"Enabled": False}

    def read(self, name, channel):
        return self.link.query(f"{name}?")

    def write(self, name, value, channel):
        self.link.send(f"{name} {value}")

    def run(self, name, args, channel):
        return self.link.query("AUTOTUNE")
```

`read` may return a string: the base class converts it to the attribute's type. Raise
`labrat.message.LabratError("device", ...)` when the equipment fails. Copy absolute maximum ratings from the
manufacturer's documentation into `min` and `max` exactly, and never loosen them.

Publish it in your own package with an entry point, and LABRAT finds it once installed:

```toml
[project.entry-points."labrat.drivers"]
acme-heater = "labrat_acme:AcmeHeater"
```

Drivers for common equipment are welcome in LABRAT itself. Include tests that run without hardware, as
`tests/test_scpi.py` does with a fake instrument, and say which instrument and firmware you used.
