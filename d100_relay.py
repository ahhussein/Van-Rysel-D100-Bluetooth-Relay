#!/usr/bin/env python3
"""
d100_relay.py - relay power from a Bluetooth FTMS smart trainer (e.g. Van Rysel D100)
to a Garmin watch or bike computer, as a standard Bluetooth Cycling Power sensor.

macOS only. MyWhoosh keeps its normal Bluetooth connection to the trainer; macOS
shares that single radio link with this script, which reads the trainer's power and
re-broadcasts it from your Mac as a power meter.

Usage:
    1. Connect the trainer in MyWhoosh as usual.
    2. python3 d100_relay.py
    3. On the watch: Sensors & Accessories > Add New > Power > "D100 Relay".

Testing without MyWhoosh:
    python3 d100_relay.py --standalone
"""
import argparse
import signal
import struct
import sys
import time

import objc
from Foundation import NSObject, NSData, NSTimer
from PyObjCTools import AppHelper
import CoreBluetooth as CB

# Bluetooth SIG UUIDs
FTMS_SERVICE = CB.CBUUID.UUIDWithString_("1826")
INDOOR_BIKE_DATA = CB.CBUUID.UUIDWithString_("2AD2")
CPS_SERVICE = CB.CBUUID.UUIDWithString_("1818")
CPS_MEASUREMENT = CB.CBUUID.UUIDWithString_("2A63")
CPS_FEATURE = CB.CBUUID.UUIDWithString_("2A65")
SENSOR_LOCATION = CB.CBUUID.UUIDWithString_("2A5D")
BATTERY_SERVICE = CB.CBUUID.UUIDWithString_("180F")
BATTERY_LEVEL = CB.CBUUID.UUIDWithString_("2A19")

POWERED_ON = 5            # CBManagerStatePoweredOn
PROP_READ = 0x02
PROP_NOTIFY = 0x10
PERM_READABLE = 0x01
STALE_AFTER_S = 3.0       # send 0 W if the trainer goes quiet for this long

CONFIG = None


def log(msg):
    sys.stdout.write("\r\033[K" + msg + "\n")
    sys.stdout.flush()


def nsdata(b: bytes):
    return NSData.dataWithBytes_length_(b, len(b))


def parse_indoor_bike_data(data: bytes):
    """Return (power_watts, cadence_rpm) from an FTMS Indoor Bike Data packet.
    Either value is None when the packet doesn't carry it."""
    if len(data) < 2:
        return None, None
    flags = int.from_bytes(data[0:2], "little")
    pos = 2
    power = cadence = None

    def take(n):
        nonlocal pos
        chunk = data[pos:pos + n]
        pos += n
        return chunk if len(chunk) == n else None

    if not flags & 0x0001:          # bit 0 cleared = instantaneous speed present
        take(2)
    if flags & 0x0002:              # average speed
        take(2)
    if flags & 0x0004:              # instantaneous cadence, 0.5 rpm units
        c = take(2)
        if c is not None:
            cadence = int.from_bytes(c, "little") / 2
    if flags & 0x0008:              # average cadence
        take(2)
    if flags & 0x0010:              # total distance
        take(3)
    if flags & 0x0020:              # resistance level
        take(2)
    if flags & 0x0040:              # instantaneous power
        p = take(2)
        if p is not None:
            power = int.from_bytes(p, "little", signed=True)
    return power, cadence


def cps_measurement(power: int, crank_revs=None, crank_evt=None) -> bytes:
    """Build a Cycling Power Measurement packet, optionally with crank data."""
    power = max(-32768, min(32767, int(power)))
    if crank_revs is None:
        return struct.pack("<Hh", 0x0000, power)
    return struct.pack("<HhHH", 0x0020, power, crank_revs & 0xFFFF, crank_evt & 0xFFFF)


class Relay(NSObject):
    def init(self):
        self = objc.super(Relay, self).init()
        if self is None:
            return None
        self.trainer = None
        self.power = 0
        self.cadence = None
        self.last_data = 0.0
        self.last_tick = time.monotonic()
        self.crank_revs = 0.0
        self.crank_evt = 0
        self.subscribers = 0
        self.cps_char = None
        self.services_added = False
        self.central = CB.CBCentralManager.alloc().initWithDelegate_queue_(self, None)
        self.pm = CB.CBPeripheralManager.alloc().initWithDelegate_queue_(self, None)
        NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            1.0, self, b"tick:", None, True)
        NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            2.0, self, b"lookForTrainer:", None, True)
        return self

    # ---------- trainer side (central) ----------

    def centralManagerDidUpdateState_(self, central):
        if central.state() == POWERED_ON:
            log("Bluetooth on. Looking for the trainer...")
            self.lookForTrainer_(None)
        else:
            log(f"Bluetooth not ready (state {central.state()}). "
                "Check Bluetooth is on and Terminal is allowed to use it.")

    def lookForTrainer_(self, timer):
        if self.trainer is not None or self.central.state() != POWERED_ON:
            return
        # A trainer already connected by MyWhoosh (or any other app on this Mac)
        connected = self.central.retrieveConnectedPeripheralsWithServices_([FTMS_SERVICE])
        if connected and len(connected) > 0:
            self._attach(connected[0], shared=True)
            return
        if CONFIG.standalone and not self.central.isScanning():
            log("No connected trainer found; scanning (standalone mode)...")
            self.central.scanForPeripheralsWithServices_options_([FTMS_SERVICE], None)
        elif not CONFIG.standalone and timer is None:
            log("Waiting for MyWhoosh to connect to the trainer...")

    def centralManager_didDiscoverPeripheral_advertisementData_RSSI_(self, central, p, adv, rssi):
        name = p.name() or adv.get(CB.CBAdvertisementDataLocalNameKey) or ""
        if CONFIG.trainer_name and CONFIG.trainer_name.lower() not in str(name).lower():
            return
        central.stopScan()
        self._attach(p, shared=False)

    @objc.python_method
    def _attach(self, p, shared):
        self.trainer = p
        p.setDelegate_(self)
        log(f"{'Sharing MyWhoosh connection to' if shared else 'Connecting to'} "
            f"trainer: {p.name() or 'unnamed'}")
        self.central.connectPeripheral_options_(p, None)

    def centralManager_didConnectPeripheral_(self, central, p):
        p.discoverServices_([FTMS_SERVICE])

    def centralManager_didFailToConnectPeripheral_error_(self, central, p, error):
        log(f"Could not connect to trainer: {error}")
        self.trainer = None

    def centralManager_didDisconnectPeripheral_error_(self, central, p, error):
        log("Trainer disconnected. Waiting for it to come back...")
        self.trainer = None

    def peripheral_didDiscoverServices_(self, p, error):
        for s in p.services() or []:
            if s.UUID().isEqual_(FTMS_SERVICE):
                p.discoverCharacteristics_forService_([INDOOR_BIKE_DATA], s)

    def peripheral_didDiscoverCharacteristicsForService_error_(self, p, service, error):
        for c in service.characteristics() or []:
            if c.UUID().isEqual_(INDOOR_BIKE_DATA):
                p.setNotifyValue_forCharacteristic_(True, c)
                log("Receiving trainer data.")

    def peripheral_didUpdateValueForCharacteristic_error_(self, p, c, error):
        if error is not None or c.value() is None:
            return
        power, cadence = parse_indoor_bike_data(bytes(c.value()))
        if power is not None:
            self.power = power
            self.last_data = time.monotonic()
        if cadence is not None:
            self.cadence = cadence

    # ---------- watch side (peripheral) ----------

    def peripheralManagerDidUpdateState_(self, pm):
        if pm.state() != POWERED_ON or self.services_added:
            return
        self.services_added = True

        self.cps_char = CB.CBMutableCharacteristic.alloc().initWithType_properties_value_permissions_(
            CPS_MEASUREMENT, PROP_NOTIFY, None, PERM_READABLE)
        # Feature: crank revolution data supported (bit 3)
        feature = CB.CBMutableCharacteristic.alloc().initWithType_properties_value_permissions_(
            CPS_FEATURE, PROP_READ, nsdata(struct.pack("<I", 0x08)), PERM_READABLE)
        location = CB.CBMutableCharacteristic.alloc().initWithType_properties_value_permissions_(
            SENSOR_LOCATION, PROP_READ, nsdata(bytes([13])), PERM_READABLE)  # 13 = rear hub
        cps = CB.CBMutableService.alloc().initWithType_primary_(CPS_SERVICE, True)
        cps.setCharacteristics_([self.cps_char, feature, location])

        # Garmin watches are happier when a battery level is present
        battery_level = CB.CBMutableCharacteristic.alloc().initWithType_properties_value_permissions_(
            BATTERY_LEVEL, PROP_READ, nsdata(bytes([100])), PERM_READABLE)
        battery = CB.CBMutableService.alloc().initWithType_primary_(BATTERY_SERVICE, True)
        battery.setCharacteristics_([battery_level])

        pm.addService_(cps)
        pm.addService_(battery)
        pm.startAdvertising_({
            CB.CBAdvertisementDataLocalNameKey: CONFIG.name,
            CB.CBAdvertisementDataServiceUUIDsKey: [CPS_SERVICE],
        })

    def peripheralManager_didAddService_error_(self, pm, service, error):
        if error is not None:
            log(f"Could not add service {service.UUID()}: {error}")

    def peripheralManagerDidStartAdvertising_error_(self, pm, error):
        if error is not None:
            log(f"Could not start advertising: {error}")
        else:
            log(f"Broadcasting as power meter '{CONFIG.name}'. Pair it on your watch.")

    def peripheralManager_central_didSubscribeToCharacteristic_(self, pm, central, c):
        self.subscribers += 1
        log("Watch connected to the relay.")

    def peripheralManager_central_didUnsubscribeFromCharacteristic_(self, pm, central, c):
        self.subscribers = max(0, self.subscribers - 1)
        log("Watch disconnected from the relay.")

    # ---------- 1 Hz output ----------

    def tick_(self, timer):
        now = time.monotonic()
        fresh = (now - self.last_data) < STALE_AFTER_S
        power = self.power if fresh else 0
        cadence = self.cadence if (fresh and self.cadence is not None) else None

        crank = None
        if cadence is not None:
            before = int(self.crank_revs)
            self.crank_revs += cadence / 60.0 * (now - self.last_tick)
            if int(self.crank_revs) != before:
                self.crank_evt = int(now * 1024) & 0xFFFF
            crank = (int(self.crank_revs), self.crank_evt)
        self.last_tick = now

        if self.subscribers and self.cps_char is not None:
            pkt = cps_measurement(power, *crank) if crank else cps_measurement(power)
            self.pm.updateValue_forCharacteristic_onSubscribedCentrals_(
                nsdata(pkt), self.cps_char, None)

        trainer = "trainer ok" if fresh else "no trainer data"
        watch = "watch connected" if self.subscribers else "watch not connected"
        cad = f" | {cadence:.0f} rpm" if cadence is not None else ""
        sys.stdout.write(f"\r\033[K{power:4d} W{cad} | {trainer} | {watch}")
        sys.stdout.flush()


def main():
    global CONFIG
    ap = argparse.ArgumentParser(description="Relay FTMS trainer power to a Garmin as a Bluetooth power meter.")
    ap.add_argument("--name", default="D100 Relay", help="name the watch will see (default: D100 Relay)")
    ap.add_argument("--standalone", action="store_true",
                    help="connect to the trainer directly when MyWhoosh isn't running (for testing)")
    ap.add_argument("--trainer-name", default="",
                    help="in standalone mode, only connect to a trainer whose name contains this text")
    CONFIG = ap.parse_args()

    relay = Relay.alloc().init()  # noqa: F841 (kept alive by the run loop)
    signal.signal(signal.SIGINT, lambda *_: AppHelper.stopEventLoop())
    print("D100 relay starting. Press Ctrl+C to stop.")
    AppHelper.runConsoleEventLoop(installInterrupt=True)
    print("\nStopped.")


if __name__ == "__main__":
    main()
