"""Tests for the touch tap/hold state machine. Runs on a PC.

    python tools/test_touch.py

Worth testing off the hardware because the failure modes are ones a person
cannot reliably produce on purpose: a second hold from one contact, a hold that
survives the finger sliding away, a tap that repeats while a finger rests. Each
of those fires something the operator did not ask for, and a hold is reserved
for the gestures that drive the tool at the work.
"""

import sys
import types
from pathlib import Path

# machine is MicroPython's. Only Pin and I2C need to exist; the I2C is replaced
# per test with one that returns crafted register reads.
_machine = types.ModuleType("machine")


class _Pin:
    OUT = 1
    IN = 0
    PULL_UP = 2

    def __init__(self, *args, **kwargs):
        pass

    def __call__(self, *args):
        return 0

    def value(self, *args):
        return 0


class _I2C:
    def __init__(self, *args, **kwargs):
        pass

    def scan(self):
        return []

    def readfrom_mem(self, *args):
        return b"\x00\x00\x00\x00\x00"


_machine.Pin = _Pin
_machine.I2C = _I2C
sys.modules.setdefault("machine", _machine)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "micropython"))

from pendant.touch import Touch, HOLD_MS, HOLD_SLOP_PX  # noqa: E402
from pendant import touch as touch_module  # noqa: E402

failures = []


def check(label, got, want):
    ok = got == want
    print("  {:<52} {}".format(label, "PASS" if ok else "FAIL"))
    if not ok:
        print("      got  {!r}\n      want {!r}".format(got, want))
        failures.append(label)


class FakePanel:
    """An FT6336U that reports whatever the test sets."""

    def __init__(self):
        self.point = None          # (x, y) while touched, None when not

    def scan(self):
        return [0x38]

    def readfrom_mem(self, address, register, length):
        if register == 0xA3:
            return b"\x64"          # chip id
        if register == 0xA8:
            return b"\x11"          # FocalTech
        if self.point is None:
            return b"\x00\x00\x00\x00\x00"
        x, y = self.point
        return bytes([1, (x >> 8) & 0x0F, x & 0xFF,
                      (y >> 8) & 0x0F, y & 0xFF])


class Clock:
    """Controls the module's view of time, so holds do not need real waiting."""

    def __init__(self):
        self.now = 100000

    def ticks_ms(self):
        return self.now

    def ticks_diff(self, a, b):
        return a - b

    def ticks_add(self, a, delta):
        return a + delta

    def sleep_ms(self, ms):
        self.now += ms


def new_touch():
    panel = FakePanel()
    clock = Clock()
    touch_module.time = clock
    device = Touch(sda=10, scl=11, rst=13, int_pin=12)
    device._i2c = panel
    device.present = True
    # Past the debounce, so the first press in a test is not swallowed.
    device._last_tap = clock.now - 10000
    return device, panel, clock


print("tap")

device, panel, clock = new_touch()
panel.point = (160, 240)
check("a press reports a tap with its position", device.poll(),
      ("tap", 160, 240))

# One tap per contact. A capacitive panel reports a touch for as long as a
# finger rests, and a pendant is held in a hand.
clock.sleep_ms(30)
result = device.poll()
check("  and a resting finger does not tap again",
      result is None or result[0] == "progress", True)

panel.point = None
check("  releasing reports nothing", device.poll(), None)

print("\nhold")

device, panel, clock = new_touch()
panel.point = (100, 300)
device.poll()                                   # the tap

# Just short of the threshold: progress, not a hold.
clock.sleep_ms(HOLD_MS - 100)
event = device.poll()
check("before the threshold it reports progress", event[0], "progress")
check("  rising towards one", 0.5 < event[1] < 1.0, True)

clock.sleep_ms(200)
check("crossing the threshold fires the hold", device.poll(),
      ("hold", 100, 300))

# The gesture is spent. A finger left down must not fire again - a probe cycle
# triggered twice from one press is the failure this guards.
clock.sleep_ms(HOLD_MS * 2)
check("  and does not fire twice from one contact", device.poll(), None)

print("\nabandoning a hold")

device, panel, clock = new_touch()
panel.point = (100, 300)
device.poll()
clock.sleep_ms(HOLD_MS - 100)

# Sliding off the target is how a hold is abandoned deliberately.
panel.point = (100 + HOLD_SLOP_PX + 20, 300)
event = device.poll()
check("sliding off the target abandons it", event, ("progress", 0.0))
clock.sleep_ms(HOLD_MS)
check("  and it cannot then fire", device.poll(), None)

# But a finger resting for the best part of a second wanders, and demanding it
# stay perfectly still would make the gesture feel broken.
device, panel, clock = new_touch()
panel.point = (100, 300)
device.poll()
clock.sleep_ms(HOLD_MS - 100)
panel.point = (100 + HOLD_SLOP_PX - 10, 300 + 5)
device.poll()
clock.sleep_ms(200)
check("a small wander still fires", device.poll()[0], "hold")

# The reported position is where the finger landed, not where it drifted to,
# so a hold acts on the target the operator aimed at.
device, panel, clock = new_touch()
panel.point = (100, 300)
device.poll()
clock.sleep_ms(HOLD_MS - 50)
panel.point = (100 + HOLD_SLOP_PX - 5, 300)
clock.sleep_ms(100)
check("  at the position it started from", device.poll(), ("hold", 100, 300))

print("\ncounters")

device, panel, clock = new_touch()
panel.point = (10, 10)
device.poll()
clock.sleep_ms(HOLD_MS + 10)
device.poll()
check("taps and holds are counted separately",
      (device.stats["taps"], device.stats["holds"]), (1, 1))

print()
if failures:
    print("{} FAILED: {}".format(len(failures), ", ".join(failures)))
    sys.exit(1)
print("all touch tests passed")
