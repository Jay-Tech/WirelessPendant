"""Tests for the battery curve. Runs on a PC, no board.

    python tools/test_battery.py

The curve replaces the AXP2101's own fuel gauge, which was measured wrong on
the ESP32-S3 board - see the note on _CURVE in battery_monitor.py. That makes
this arithmetic the only thing standing between a flat cell and a pendant that
says it is fine, so it is worth testing off the hardware where every value can
be tried rather than only the one the bench cell happens to be at.
"""

import sys
import types
from pathlib import Path

# `machine` does not exist off the board, and battery_monitor imports it at
# module scope. Stubbed rather than guarded, the same way test_screen stubs the
# display driver: the I2C is not what this is testing, and a guard in the
# module would be untested code shipped for the benefit of the tests.
_machine = types.ModuleType("machine")
_machine.I2C = object
_machine.Pin = object
sys.modules.setdefault("machine", _machine)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "micropython"))

from pendant.battery_monitor import percent_from_voltage, _CURVE  # noqa: E402

failures = []


def check(label, got, want):
    ok = got == want
    print("  {:<52} {}".format(label, "PASS" if ok else "FAIL"))
    if not ok:
        print("      got  {!r}\n      want {!r}".format(got, want))
        failures.append(label)


print("\nthe ends of the curve, where a warning has to be right")

check("a full cell reads 100", percent_from_voltage(4200), 100)
check("  and anything above it still reads 100",
      percent_from_voltage(4350), 100)
check("an empty cell reads 0", percent_from_voltage(3270), 0)

# The empty connector on this board measures 42 mV, established by unplugging
# the cell and dumping the registers. That must not come out as 0%, which would
# put a confident "flat battery" on the panel for a pendant that simply has no
# cell in it - a different problem with a different fix.
check("an empty connector reads nothing at all",
      percent_from_voltage(42), None)
check("  as does anything below the curve",
      percent_from_voltage(3269), None)
check("  and so does a missing reading", percent_from_voltage(None), None)


print("\nin between")

# 4.019 V is what the bench cell measured against a meter's 3.998 V, so this is
# the one point on the curve with a physical reading behind it rather than a
# table lookup. Around 80% is right for a LiPo there - and the PMIC's own gauge
# said 31% at this voltage, which is the whole reason this function exists.
check("the bench cell at 4.019 V reads about 80",
      78 <= percent_from_voltage(4019) <= 82, True)
check("  the midpoint of the flat region is near half",
      45 <= percent_from_voltage(3840) <= 55, True)
check("  a nearly flat cell warns rather than reassures",
      percent_from_voltage(3700) <= 15, True)

# Every table entry has to return its own value, or the interpolation is
# skewed against the points it is built from.
for _millivolts, _percent in _CURVE:
    if _millivolts >= _CURVE[0][0]:
        continue
    check("  {} mV reads its tabulated {}%".format(_millivolts, _percent),
          percent_from_voltage(_millivolts), _percent)


print("\nshape")

# A curve that goes backwards anywhere would show a charging cell losing
# charge, which is the exact symptom that condemned the PMIC's gauge.
_previous = None
_monotonic = True
for _millivolts in range(3270, 4210, 5):
    _value = percent_from_voltage(_millivolts)
    if _previous is not None and _value < _previous:
        _monotonic = False
        break
    _previous = _value
check("charge never falls as voltage rises", _monotonic, True)

_in_range = all(0 <= percent_from_voltage(mv) <= 100
                for mv in range(3270, 4210, 5))
check("  and never leaves 0-100", _in_range, True)

print()
if failures:
    print("{} FAILED: {}".format(len(failures), ", ".join(failures)))
    sys.exit(1)
print("all battery curve tests passed")
