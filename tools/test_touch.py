"""Tests for touch zone hit-testing. Runs on a PC.

    python tools/test_touch.py

The geometry is worth testing off the hardware because a mis-registered zone is
invisible: the label is drawn in the right place, the target is somewhere else,
and the only symptom is a screen that ignores you while standing at a machine.
"""

import sys
import types
from pathlib import Path

# machine is MicroPython's. Touch only needs I2C and Pin to exist for import;
# Zones, which is what is under test here, needs neither.
_machine = types.ModuleType("machine")


class _Stub:
    def __init__(self, *args, **kwargs):
        pass

    def __call__(self, *args):
        return 0

    def scan(self):
        return []


_machine.Pin = _Stub
_machine.I2C = _Stub
sys.modules.setdefault("machine", _machine)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "micropython"))

from pendant.touch import Zones  # noqa: E402

failures = []


def check(label, got, want):
    ok = got == want
    print("  {:<52} {}".format(label, "PASS" if ok else "FAIL"))
    if not ok:
        print("      got  {!r}\n      want {!r}".format(got, want))
        failures.append(label)


print("zone hit-testing")

zones = Zones()
zones.add("axis", 0, 20, 320, 46, "X")
zones.add("axis", 0, 66, 320, 46, "Y")
zones.add("axis", 0, 112, 320, 46, "Z")

check("a tap inside a row selects it", zones.hit(160, 40), ("axis", "X"))
check("  and the row below is a different one", zones.hit(160, 80),
      ("axis", "Y"))
check("  and the third", zones.hit(160, 130), ("axis", "Z"))

# Half-open bounds, so adjacent zones tile without overlapping or leaving a
# dead line between them. A row ending at 66 and the next starting at 66 must
# resolve to exactly one.
check("the boundary belongs to the lower row", zones.hit(160, 66),
      ("axis", "Y"))
check("  and the row above ends just before it", zones.hit(160, 65),
      ("axis", "X"))

check("a tap above everything hits nothing", zones.hit(160, 5), None)
check("  as does one below", zones.hit(160, 400), None)
check("  and one outside the width", zones.hit(400, 40), None)

print("\noverlap")

# Later wins, because that is what the operator sees: anything drawn on top of
# something else is the thing they are aiming at.
stacked = Zones()
stacked.add("under", 0, 0, 200, 100, "background")
stacked.add("over", 50, 25, 100, 50, "button")
check("the zone drawn last answers", stacked.hit(100, 50), ("over", "button"))
check("  while outside it the one beneath still does",
      stacked.hit(10, 50), ("under", "background"))

print("\nrebuild")

stacked.clear()
check("clearing removes every zone", stacked.hit(100, 50), None)

print("\ntouch target size")

# The panel is 49.56 mm across 320 px, so about 6.46 px/mm. A fingertip needs
# 9-10 mm to be hit reliably without looking, and a pendant is used without
# looking. This is the arithmetic that decides how many zones fit across.
PX_PER_MM = 320 / 49.56
for count in (3, 5, 6):
    width_px = 320 / count
    print("  {} across: {:.0f} px = {:.1f} mm{}".format(
        count, width_px, width_px / PX_PER_MM,
        "" if width_px / PX_PER_MM >= 9 else "   <- below a fingertip"))

check("three across clears a fingertip", (320 / 3) / PX_PER_MM >= 9, True)
check("  five across is at the limit but usable",
      8.0 <= (320 / 5) / PX_PER_MM < 11.0, True)

print()
if failures:
    print("{} FAILED: {}".format(len(failures), ", ".join(failures)))
    sys.exit(1)
print("all touch zone tests passed")
