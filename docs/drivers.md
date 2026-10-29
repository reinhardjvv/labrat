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

Oscilloscopes, VNAs and spectrum analysers send waveforms and traces as binary blocks or as lists of numbers.
A function reads either:

```toml
[devices.scope.functions.Waveform]
query = "CURVE?"
block = "int16"                      # int8, uint8, int16, uint16, int32, float32, float64, or bytes for base64
byte_order = "big"

[devices.vna.functions.Trace]
query = "CALC:DATA? FDATA"
parse = "floats"                     # or ints
```

Set the instrument's data format with a `send` function or its own configuration first, as its programming
manual describes. The reply is a list of numbers.

The driver asks `*IDN?` once for the instrument's manufacturer, model, serial and firmware. Set the `kind`
yourself (`oscilloscope`, `vna`, `dc-power` ...), and `identify = false` for an instrument without `*IDN?`
([naming.md](naming.md)).

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

## Profiles

Any device can take its settings from a [profile](catalog.md) in the catalog, with only the port added:
`profile = "mecer/me-2000-wpru"` and `port = "hid:///dev/hidraw0"`.

## serial

Serial devices with their own text commands: pumps, stages, controllers, Arduino boards. Attributes,
functions, raw commands and safe commands work as in `scpi`. Needs `labrat-lab[serial]`.

```toml
[devices.pump]
driver = "serial"
port = "/dev/ttyUSB0"                  # or socket://192.0.2.40:4001 and rfc2217://... for serial servers
baud = 9600
parity = "none"                        # none, even or odd; data_bits and stop_bits as well
line_ending = "crlf"                   # after each command: lf, cr or crlf
reply_ending = "cr"                    # if the device ends its replies differently
safe_commands = ["STOP"]

[devices.pump.attributes.Rate]
type = "float"
unit = "ml/min"
min = 0
max = 50
get = "RAT?"
reply = "RAT=([-0-9.]+)"               # the value is the first group
set = "RAT {value}"

[devices.pump.attributes.Running]
type = "bool"
get = "STATUS"
reply = "RUN=(\\w+)"
on = "YES"
off = "NO"
set = "{value}"
```

The device is not asked `*IDN?` unless `identify = true`. `reply` works in the `scpi` driver too.

Over a USB HID bridge, as many UPSs use, give `port = "hid:///dev/hidraw0"`. With a profile and
`simulate = true` the device answers from the profile's recordings ([catalog](catalog.md)).

## frames

Binary protocols: Modbus RTU and TCP, and fixed frames with a checksum, as many meters, loads and controllers
use. Needs `labrat-lab[serial]` for serial ports.

```toml
[devices.meter]
driver = "frames"
protocol = "modbus-rtu"                # or modbus-tcp, with address = "tcp://192.0.2.60:502"
port = "/dev/ttyUSB0"
baud = 9600
unit = 1

[devices.meter.attributes.Voltage]
register = 0                           # a holding register; input = true for an input register
type = "u16"                           # u16, i16, u32, i32 or f32
scale = 0.1
unit = "V"

[devices.meter.attributes.Relay]
coil = 5
access = "rw"

[devices.load]
driver = "frames"
port = "/dev/ttyUSB1"
checksum = "sum8"                      # none, sum8, xor8, crc16-modbus or crc16-ccitt

[devices.load.attributes.Current]
send = "AA 01 01 {checksum}"           # hex, with {checksum} and {value:<type>} fields
reply_length = 6
check_reply = true
field = { offset = 3, type = "u16be" }
scale = 0.001
unit = "A"
set = "AA 02 01 {value:u16be} {checksum}"
```

Modbus replies are checked by their CRC and exceptions come back as errors. Values that do not fit their
register or field are refused.

## i2c

I2C sensors, ADCs, DACs and port expanders on a rat's bus, through smbus2 (`labrat-lab[i2c]`). Each attribute
is a register, with its size, byte order, sign, bit field and scaling to engineering units.

```toml
[devices.climate]
driver = "i2c"
kind = "sensor"
bus = 1                                # /dev/i2c-1
address = 0x48
init = [[0x01, 0x60]]                  # register and byte written at start

[devices.climate.attributes.Temperature]
register = 0x00
size = 2                               # 1 to 4 bytes
byte_order = "big"
signed = true
shift = 4                              # the value is the top 12 bits
bits = 12
scale = 0.0625                         # units per count
unit = "°C"

[devices.dac.attributes.Output]
register = 0x40
size = 2
scale = 0.001
unit = "V"
min = 0
max = 3.3
access = "rw"
```

Attributes are read-only unless `access = "rw"`. A value that does not fit the register is refused, and a bit
field is written without touching the other bits. Without a register an attribute reads and writes one byte,
for expanders such as the PCF8574. `raw = "read"` adds `ReadBytes`, and `raw = "on"` adds `WriteBytes`.
On Raspberry Pi OS, enable I2C with `raspi-config` and add the user to the `i2c` group.

## mqtt

Anything on an MQTT broker: Zigbee2MQTT, Tasmota and Shelly lights and plugs, ESP32 boards, sensors and
building systems. Needs `labrat-lab[mqtt]`.

```toml
[devices.lamp]
driver = "mqtt"
kind = "light"
broker = "mqtt://192.0.2.10:1883"      # mqtts:// for TLS
username = "labrat"
password_env = "LAB_MQTT_PASSWORD"     # read from the environment
safe = { Power = false }

[devices.lamp.attributes.Power]
type = "bool"
state = "zigbee2mqtt/lamp"             # where the device reports
json = "state"                         # the field in its message
command = "zigbee2mqtt/lamp/set"       # where to send changes
payload = '{"state": "{value}"}'
on = "ON"
off = "OFF"

[devices.lamp.functions.Blink]
topic = "zigbee2mqtt/lamp/set"
payload = '{"effect": "blink"}'
```

A read gives the last message on the state topic, waiting up to `timeout` (5 s) for the first one. An
attribute with only a command topic reads back the value last written. The driver reconnects by itself.

## http

Anything with a web API: Shelly plugs, WLED strips, Hue bridges, network sensors and services. No extra
packages.

```toml
[devices.strip]
driver = "http"
kind = "light"
url = "http://192.0.2.30"
headers = { Authorization = "Bearer ${STRIP_TOKEN}" }   # ${NAME} from the environment
safe = { On = false }

[devices.strip.attributes.On]
type = "bool"
get = "/json/state"                    # GET, then the field in the JSON reply
json = "on"
set = "POST /json/state"
body = '{"on": {value}}'

[devices.strip.functions.Preset]
request = "POST /json/state"
body = '{"ps": {id}}'
args = { id = "int" }
```

`json` takes a path such as `lights.0.on`, and `reply` a regular expression for plain-text replies.
Booleans go out as `true` and `false` unless `on` and `off` say otherwise. HTTPS certificates are checked
unless `verify = false`.

## nut

Uninterruptible power supplies through Network UPS Tools, on the nest, a rat or a NAS that runs `upsd`. No
extra packages.

```toml
[devices.ups]
driver = "nut"
host = "127.0.0.1"
ups = "lab"                            # the name in NUT's ups.conf
commands = ["beeper.mute", "test.battery.start.quick"]
username = "labrat"                    # a upsd.users entry allowed those commands
password_env = "LAB_UPS_PASSWORD"
```

Every UPS reads `BatteryCharge`, `BatteryRuntime`, `Load`, `InputVoltage`, `OutputVoltage`, `Status`,
`OnBattery` and `LowBattery`, and `Variables` returns all it reports. `variables = { Name = "nut.variable" }`
adds more. Instant commands are off unless listed, so a shutdown is never one call away by accident. The UPS
reports its maker, model and serial.

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
