# D100 Relay

Relays power from a single-connection Bluetooth trainer (Van Rysel D100) to a Garmin
watch, so the watch can record indoor rides natively (VO2 max, training load) while
MyWhoosh controls the trainer.

    D100 ──BLE──> Mac ──> MyWhoosh (controls trainer)
                   └──> d100_relay.py ──BLE power meter──> Garmin watch

## Setup (once)

    cd ~/dev/d100-relay
    python3 -m venv .venv
    source .venv/bin/activate
    pip install pyobjc-framework-CoreBluetooth

The first run asks for Bluetooth permission for Terminal. If it doesn't ask, enable it
under System Settings > Privacy & Security > Bluetooth.

## Each ride

1. Open MyWhoosh and connect the D100 as usual.
2. Run: `source .venv/bin/activate && python3 d100_relay.py`
3. On the watch, start an Indoor Bike activity. (First time only: pair the sensor via
   Sensors & Accessories > Add New > Power > "D100 Relay".)

## Testing without MyWhoosh

    python3 d100_relay.py --standalone

Connects to the trainer directly, so you can check pairing with the watch.

## Notes
- The D100 has no cadence, so the watch gets power only. Pair a cadence sensor and HR
  strap directly to the watch if you want those.
- Keep the script running for the whole ride; if it stops, the watch sees 0 W.
