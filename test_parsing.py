"""Quick checks for the packet parsing/building (runs anywhere, no Bluetooth needed)."""
import struct, sys, types

# Stub out the macOS-only modules so the pure functions can be imported anywhere.
for name in ("objc", "Foundation", "PyObjCTools", "PyObjCTools.AppHelper", "CoreBluetooth"):
    sys.modules.setdefault(name, types.ModuleType(name))
sys.modules["Foundation"].NSObject = object
sys.modules["Foundation"].NSData = object
sys.modules["Foundation"].NSTimer = object
sys.modules["PyObjCTools"].AppHelper = sys.modules["PyObjCTools.AppHelper"]
class _UUID:
    @staticmethod
    def UUIDWithString_(s): return s
sys.modules["CoreBluetooth"].CBUUID = _UUID

from d100_relay import parse_indoor_bike_data, cps_measurement

# speed + power (flags: bit6 set, bit0 clear -> speed present)
pkt = struct.pack("<HHh", 0x0040, 2500, 215)
assert parse_indoor_bike_data(pkt) == (215, None), parse_indoor_bike_data(pkt)
# speed + cadence + resistance + power
pkt = struct.pack("<HHHhh", 0x0064, 2500, 180, 5, 250)
assert parse_indoor_bike_data(pkt) == (250, 90.0), parse_indoor_bike_data(pkt)
# "more data" packet without speed, with distance then power
pkt = struct.pack("<H", 0x0051) + (1234).to_bytes(3, "little") + struct.pack("<h", 180)
assert parse_indoor_bike_data(pkt) == (180, None), parse_indoor_bike_data(pkt)
# truncated packet doesn't crash
assert parse_indoor_bike_data(struct.pack("<H", 0x0040)) == (None, None)
assert cps_measurement(215) == struct.pack("<Hh", 0, 215)
assert cps_measurement(215, 10, 2048) == struct.pack("<HhHH", 0x20, 215, 10, 2048)
print("all parsing tests passed")
