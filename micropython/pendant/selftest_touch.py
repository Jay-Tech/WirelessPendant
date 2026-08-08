"""Bring-up test for the FT6336U capacitive touch panel - no display needed.

Run it before wiring anything to the touch layer in the pendant, because it
separates the three ways this fails: nothing on the bus, something on the bus
that is not the part we think, and a part that answers but reports no touches.
Those look identical once touch is buried inside the UI.

    python tools/on_board.py micropython/pendant/selftest_touch.py

Wiring, from the MSP3525/MSP3526 manual:

    CTP_SDA -> GP10      CTP_SCL -> GP11
    CTP_INT -> GP12      CTP_RST -> GP13
    VCC     -> VBUS (5V)

VCC is 5V deliberately. The module regulates it to 3.3V on board, and the
manual is explicit that feeding it 3.3V instead leaves the rail below 3.3 and
dims the backlight. The touch I2C lines are level converted on the module, so
the Pico's 3.3V logic is safe against them.
"""

import time

from machine import I2C, Pin

SDA_PIN, SCL_PIN = 10, 11
INT_PIN, RST_PIN = 12, 13
I2C_ID = 1

# FocalTech parts answer here. Fixed, with no alternate address strapping.
FT6336U_ADDR = 0x38

# Register map, from the FT6x36 family. Only what a bring-up needs.
REG_TD_STATUS = 0x02      # number of points currently touched
REG_P1_XH = 0x03          # first point: X high (event flag in bits 7:6)
REG_CHIPID = 0xA3
REG_VENDORID = 0xA8

FOCALTECH_VENDOR = 0x11

# Chip IDs seen on this module family. Unrecognised is reported rather than
# refused - the module has been revised before, and a working part with an
# unfamiliar ID is far more likely than a broken test.
KNOWN_CHIPS = {
    0x64: "FT6336U",
    0x36: "FT6236",
    0x06: "FT6206",
}

POLL_MS = 50
WATCH_SECONDS = 20


def reset_panel():
    """Pulse the touch controller's reset, then let it boot.

    The FT6x36 needs its reset released before it will answer, and a module
    left unpowered between runs can come up with reset asserted. Skipping this
    presents as a silent bus.
    """
    rst = Pin(RST_PIN, Pin.OUT, value=0)
    time.sleep_ms(10)
    rst.value(1)
    time.sleep_ms(300)


def scan(bus):
    found = bus.scan()
    print("bus scan: {} device(s)".format(len(found)))
    for address in found:
        note = " <- touch controller" if address == FT6336U_ADDR else ""
        print("  0x{:02X}{}".format(address, note))
    return found


def identify(bus):
    try:
        chip = bus.readfrom_mem(FT6336U_ADDR, REG_CHIPID, 1)[0]
        vendor = bus.readfrom_mem(FT6336U_ADDR, REG_VENDORID, 1)[0]
    except OSError as exc:
        print("could not read ID registers: {}".format(exc))
        return False

    name = KNOWN_CHIPS.get(chip, "unrecognised")
    print("chip id  0x{:02X}  ({})".format(chip, name))
    print("vendor   0x{:02X}  ({})".format(
        vendor, "FocalTech" if vendor == FOCALTECH_VENDOR else "unexpected"))
    return True


def read_points(bus):
    """Points currently touched, as a list of (x, y).

    One block read rather than a register at a time: the controller updates
    these together, and reading them separately can straddle an update and
    return an X from one touch with a Y from the next.
    """
    data = bus.readfrom_mem(FT6336U_ADDR, REG_TD_STATUS, 11)
    count = data[0] & 0x0F
    points = []
    for index in range(min(count, 2)):
        base = 1 + index * 6
        x = ((data[base] & 0x0F) << 8) | data[base + 1]
        y = ((data[base + 2] & 0x0F) << 8) | data[base + 3]
        points.append((x, y))
    return points


def main():
    print("\nFT6336U touch self-test")
    print("=" * 46)
    print("SDA GP{}  SCL GP{}  INT GP{}  RST GP{}".format(
        SDA_PIN, SCL_PIN, INT_PIN, RST_PIN))

    reset_panel()
    interrupt = Pin(INT_PIN, Pin.IN, Pin.PULL_UP)

    bus = I2C(I2C_ID, sda=Pin(SDA_PIN), scl=Pin(SCL_PIN), freq=400000)
    found = scan(bus)

    if FT6336U_ADDR not in found:
        print("\nno controller at 0x{:02X}.".format(FT6336U_ADDR))
        print("  Nothing at all on the bus means SDA/SCL are swapped, not")
        print("  connected, or the module is unpowered. Other addresses but")
        print("  not this one means the bus is fine and the touch FPC is not")
        print("  seated - it is a separate flip-lock connector from the")
        print("  display ribbon and is easy to leave open.")
        return

    print()
    if not identify(bus):
        return

    print("\ntouch the panel - {} seconds".format(WATCH_SECONDS))
    print("INT idles high and pulls low while touched.\n")

    deadline = time.ticks_add(time.ticks_ms(), WATCH_SECONDS * 1000)
    extremes = [9999, 9999, 0, 0]      # min x, min y, max x, max y
    seen = 0
    last = None

    while time.ticks_diff(deadline, time.ticks_ms()) > 0:
        try:
            points = read_points(bus)
        except OSError as exc:
            print("read failed: {}".format(exc))
            time.sleep_ms(200)
            continue

        if points:
            seen += 1
            x, y = points[0]
            extremes[0] = min(extremes[0], x)
            extremes[1] = min(extremes[1], y)
            extremes[2] = max(extremes[2], x)
            extremes[3] = max(extremes[3], y)
            report = "{},{}".format(x, y)
            if report != last:
                last = report
                print("  touch {:>4},{:<4}  points={}  INT={}".format(
                    x, y, len(points), interrupt.value()))
        time.sleep_ms(POLL_MS)

    print("\n{} sample(s) with a touch".format(seen))
    if not seen:
        print("  The controller answered but never reported a point. That is")
        print("  the touch layer itself - check the 6P FPC at the panel end.")
        return

    print("  x ranged {} to {}".format(extremes[0], extremes[2]))
    print("  y ranged {} to {}".format(extremes[1], extremes[3]))
    print()
    print("  Sweep corner to corner to see the full range. Which axis reaches")
    print("  ~480 and which ~320 is what decides the rotation the layout needs,")
    print("  and it is a property of how the panel was bonded rather than")
    print("  anything the datasheet settles.")


main()
