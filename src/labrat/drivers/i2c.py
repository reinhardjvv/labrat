"""I2C devices on a rat's bus: temperature and humidity sensors, ADCs, DACs, port expanders, drivers.

    [devices.climate]
    driver = "i2c"
    kind = "sensor"
    bus = 1                                # /dev/i2c-1
    address = 0x48
    init = [[0x01, 0x60]]                  # register and byte written at start, to configure the device

    [devices.climate.attributes.Temperature]
    register = 0x00
    size = 2                               # bytes: 1, 2, 3 or 4
    byte_order = "big"
    signed = true
    shift = 4                              # the value sits in the top 12 bits
    bits = 12
    scale = 0.0625                         # engineering units per count
    unit = "°C"

    [devices.dac.attributes.Output]
    register = 0x40
    size = 2
    scale = 0.001                          # 1 mV per count
    unit = "V"
    min = 0
    max = 3.3
    access = "rw"

An attribute without a register reads or writes a single byte, as port expanders such as the PCF8574 need.
A field (shift and bits) is written by reading the register and changing only those bits. Raw access is off.
raw = "read" adds ReadBytes, and raw = "on" adds WriteBytes as well, which the limits cannot check.
Needs smbus2 (pip install 'labrat-lab[i2c]') and access to /dev/i2c-*, which on Raspberry Pi OS means
membership of the i2c group.
"""
from __future__ import annotations

from ..device import Attr, Device, Function
from ..message import LabratError


class I2c(Device):
    kind = "sensor"

    def __init__(self, name: str, config: dict | None = None):
        super().__init__(name, config)
        try:
            from smbus2 import SMBus
        except ImportError:
            raise LabratError("unavailable", "the i2c driver needs smbus2: pip install 'labrat-lab[i2c]'") from None
        c = self.config
        if c.get("address") is None:
            raise LabratError("bad_request", f"{name}: an i2c device needs an address, such as 0x48")
        self.address = int(c["address"])
        if not 0x03 <= self.address <= 0x77:
            raise LabratError("bad_request", f"{name}: address 0x{self.address:02x} is outside 0x03 to 0x77")
        self.raw = str(c.get("raw", "off"))
        if self.raw not in ("off", "read", "on"):
            raise LabratError("bad_request", f"{name}: raw is 'off', 'read' or 'on'")
        self.specs: dict[str, dict] = {}
        for attr_name, spec in (c.get("attributes") or {}).items():
            spec = dict(spec)
            size = int(spec.get("size", 1))
            if size not in (1, 2, 3, 4):
                raise LabratError("bad_request", f"{name}.{attr_name}: size is 1, 2, 3 or 4 bytes")
            if spec.get("byte_order", "big") not in ("big", "little"):
                raise LabratError("bad_request", f"{name}.{attr_name}: byte_order is 'big' or 'little'")
            if "register" not in spec and size != 1:
                raise LabratError("bad_request", f"{name}.{attr_name}: without a register only one byte can be read")
            bits = spec.get("bits")
            if bits is not None and not 1 <= int(bits) + int(spec.get("shift", 0)) <= size * 8:
                raise LabratError("bad_request", f"{name}.{attr_name}: shift and bits do not fit in {size} byte(s)")
            spec.setdefault("access", "r")
            spec.setdefault("type", "bool" if bits == 1 else "float" if "scale" in spec else "int")
            self.attrs[attr_name] = Attr.from_dict(attr_name, spec)
            self.specs[attr_name] = spec
        if self.raw != "off":
            self.functions["ReadBytes"] = Function("ReadBytes", "read length bytes from a register", {"register": "int", "length": "int"})
        if self.raw == "on":
            self.functions["WriteBytes"] = Function("WriteBytes", "write bytes (hex, such as '0a ff') to a register; "
                                                    "the limits cannot check it", {"register": "int", "data": "str"})
        bus = c.get("bus", 1)
        try:
            self.bus = SMBus(bus if isinstance(bus, int) else str(bus))
        except (OSError, FileNotFoundError) as e:
            raise LabratError("unavailable", f"{name}: cannot open I2C bus {bus}: {e}") from None
        for reg, value in c.get("init") or []:
            self.bus.write_byte_data(self.address, int(reg), int(value))

    # -- registers ------------------------------------------------------------------------------------------
    def _read_raw(self, spec: dict) -> int:
        size, order = int(spec.get("size", 1)), spec.get("byte_order", "big")
        if "register" not in spec:
            return self.bus.read_byte(self.address)
        if size == 1:
            return self.bus.read_byte_data(self.address, int(spec["register"]))
        data = bytes(self.bus.read_i2c_block_data(self.address, int(spec["register"]), size))
        return int.from_bytes(data, order)

    def _write_raw(self, spec: dict, raw: int) -> None:
        size, order = int(spec.get("size", 1)), spec.get("byte_order", "big")
        if "register" not in spec:
            return self.bus.write_byte(self.address, raw & 0xFF)
        if size == 1:
            return self.bus.write_byte_data(self.address, int(spec["register"]), raw & 0xFF)
        self.bus.write_i2c_block_data(self.address, int(spec["register"]), list(raw.to_bytes(size, order)))

    def read(self, name, channel):
        spec = self.specs[name]
        raw, width = self._read_raw(spec), int(spec.get("size", 1)) * 8
        if spec.get("bits") is not None:
            width = int(spec["bits"])
            raw = (raw >> int(spec.get("shift", 0))) & ((1 << width) - 1)
        if spec.get("signed") and raw >= 1 << (width - 1):
            raw -= 1 << width
        if self.attrs[name].type == "bool":
            return bool(raw)
        v = raw * float(spec.get("scale", 1)) + float(spec.get("offset", 0))
        return round(v) if self.attrs[name].type == "int" else v

    def write(self, name, value, channel):
        spec = self.specs[name]
        counts = round((float(value) - float(spec.get("offset", 0))) / float(spec.get("scale", 1)))
        width = int(spec["bits"]) if spec.get("bits") is not None else int(spec.get("size", 1)) * 8
        lo, hi = (-(1 << (width - 1)), (1 << (width - 1)) - 1) if spec.get("signed") else (0, (1 << width) - 1)
        if not lo <= counts <= hi:
            raise LabratError("limit", f"{self.name}.{name} {value} does not fit in the register ({lo} to {hi} counts)")
        counts &= (1 << width) - 1
        if spec.get("bits") is not None:
            shift = int(spec.get("shift", 0))
            mask = ((1 << width) - 1) << shift
            counts = (self._read_raw(spec) & ~mask) | (counts << shift)    # change only this field
        self._write_raw(spec, counts)

    def run(self, name, args, channel):
        reg = int(args.get("register", 0))
        if not 0 <= reg <= 0xFF:
            raise LabratError("bad_request", "register is 0 to 255")
        if name == "ReadBytes":
            n = int(args.get("length", 1))
            if not 1 <= n <= 32:
                raise LabratError("bad_request", "length is 1 to 32 bytes")
            return list(self.bus.read_i2c_block_data(self.address, reg, n))
        try:
            data = bytes.fromhex(str(args.get("data", "")))
        except ValueError:
            raise LabratError("bad_request", "data is hex bytes, such as '0a ff'") from None
        if not 1 <= len(data) <= 32:
            raise LabratError("bad_request", "data is 1 to 32 bytes")
        self.bus.write_i2c_block_data(self.address, reg, list(data))
        return None

    def close(self) -> None:
        self.bus.close()
