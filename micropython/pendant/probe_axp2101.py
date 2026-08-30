"""Confirm the AXP2101's register map against the board, not the datasheet.

`axp2101.py` is written from XPowersLib's register numbers. They are almost
certainly right, and "almost certainly" is how a pendant ends up reporting a
flat cell as half full. This proves them, in the two ways that are available:

  1. **Identity.** The chip id must read 0x4A. Everything else is meaningless
     if it does not.
  2. **Whether a reading holds still.** A single sample cannot tell a good
     reading from a plausible one. Sampling for half a minute can: this is what
     showed the fuel gauge sliding 82% to 59% while the cell sat at 4.01 V on
     charge, which no individual reading would have given away.

**Nothing here asks for a hand mid-run.** Three earlier versions did, and all
three failed identically - this board is driven through `mpremote`, whose
output does not reach the operator until the run has finished, so a prompt
printed during a run is read after it is over. Anything needing the cell moved
belongs in probe_axp2101_map.py, which compares two separate runs and so has
no clock in it at all.

The one reading it cannot check by itself is the voltage. Put a meter on the
cell while this runs and compare - agreement within a few tens of millivolts
means the two data registers, the mask and the scaling are all right, and that
is four things confirmed by one number.

    python tools/on_board.py micropython/pendant/probe_axp2101.py

Writes three enable bits and nothing else. See the note at the top of
axp2101.py about why this file does not go near the rails.
"""

import time
from machine import I2C, Pin

try:
    from axp2101 import AXP2101, CHIP_ID
except ImportError:
    from pendant.axp2101 import AXP2101, CHIP_ID

I2C_SCL, I2C_SDA = 7, 8
ADDRESS = 0x34

SAMPLE_SECONDS = 30
SAMPLE_INTERVAL_MS = 500

# Read raw as well as decoded. When a decoded field looks wrong, the question
# is immediately whether the register moved or the decoding did, and only the
# raw byte answers that.
WATCHED = (
    (0x00, "STATUS1  vbus/batfet/present"),
    (0x01, "STATUS2  charge direction"),
    (0x18, "COMMON   gauge enable"),
    (0x30, "ADC      channel enables"),
    (0x68, "BATDET   battery detection"),
)


def bits(value):
    return "{:08b}".format(value)


def dump(pmu, label):
    print("\n{}".format(label))
    for register, name in WATCHED:
        value = pmu._read(register)
        print("  {:#04x}  {:<32} {:#04x}  {}".format(
            register, name, value, bits(value)))


def main():
    print("\nAXP2101 probe")
    print("=" * 60)

    i2c = I2C(0, scl=Pin(I2C_SCL), sda=Pin(I2C_SDA), freq=400_000)

    if ADDRESS not in i2c.scan():
        print("  nothing at {:#04x} on GPIO{}/{} - wrong board or dead bus".format(
            ADDRESS, I2C_SCL, I2C_SDA))
        return 1

    pmu = AXP2101(i2c, ADDRESS)
    chip = pmu.chip_id()
    print("  chip id {:#04x}, expected {:#04x}  {}".format(
        chip, CHIP_ID, "OK" if chip == CHIP_ID else "MISMATCH"))
    if chip != CHIP_ID:
        print("  stop here. Every register below means something else on")
        print("  whatever this part actually is.")
        return 1

    dump(pmu, "before enabling anything")
    pmu.begin()
    dump(pmu, "after begin() - detect, gauge and battery ADC")

    print("\nsampling for {} s. Watch whether the numbers hold still - a".format(
        SAMPLE_SECONDS))
    print("voltage that wanders or a percentage that slides is the finding.")
    print()
    print("  {:>5}  {:>7}  {:>5}  {:>4}  {:>9}  {:>10}".format(
        "t", "volts", "pct", "batt", "charge", "STATUS1"))

    # Every distinct STATUS1/STATUS2 pair seen, in the order first seen. This
    # is the actual output of the probe: the bits that moved are the bits that
    # report the thing that was changed, and the ones that sat still are not
    # what they are labelled.
    seen = []
    deadline = time.ticks_add(time.ticks_ms(), SAMPLE_SECONDS * 1000)
    start = time.ticks_ms()

    while time.ticks_diff(deadline, time.ticks_ms()) > 0:
        status1 = pmu._read(0x00)
        status2 = pmu._read(0x01)
        if (status1, status2) not in seen:
            seen.append((status1, status2))

        connected = pmu.is_battery_connected()
        millivolts = pmu.get_battery_voltage()
        percent = pmu.get_battery_percentage()
        state = pmu.charge_state()

        print("  {:>5.1f}  {:>7}  {:>5}  {:>4}  {:>9}  {}".format(
            time.ticks_diff(time.ticks_ms(), start) / 1000.0,
            "{:.3f}".format(millivolts / 1000.0) if millivolts else "-",
            "{}%".format(percent) if percent is not None else "-",
            "yes" if connected else "no",
            ("standby", "charging", "discharging", "?")[state],
            bits(status1)))

        time.sleep_ms(SAMPLE_INTERVAL_MS)

    print("\ndistinct status pairs seen, in order:")
    for status1, status2 in seen:
        print("  STATUS1 {}   STATUS2 {}".format(bits(status1), bits(status2)))

    if len(seen) < 2:
        print("\n  One pair throughout, which is what a board left alone")
        print("  should show. Status moving on its own is what would matter.")
    else:
        changed1 = 0
        changed2 = 0
        for status1, status2 in seen[1:]:
            changed1 |= status1 ^ seen[0][0]
            changed2 |= status2 ^ seen[0][1]
        print("\n  STATUS1 bits that moved: {}".format(bits(changed1)))
        print("  STATUS2 bits that moved: {}".format(bits(changed2)))
        print()
        print("  Status changed with nobody touching anything. Bit 3 of")
        print("  STATUS1 is battery present and bits 6:5 of STATUS2 are the")
        print("  charge direction, both confirmed on this board - so a move")
        print("  here is the cell or the charger doing something real.")

    return 0


raise SystemExit(main())
