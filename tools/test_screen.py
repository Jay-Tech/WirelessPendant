"""Tests for the DRO layout. Runs on a PC against a fake display.

    python tools/test_screen.py

The layout is worth testing off the hardware because the failure mode is
silent: a field placed past the bottom edge is clipped by the driver rather
than raising, so a panel swap can hide a whole status line and the only symptom
is a pendant that looks fine until you need the thing that is missing.
"""

import sys
import types
from pathlib import Path

# The driver is stubbed rather than imported. Importing it drags in machine,
# framebuf and const - none of which exist off the board - and none of which
# this is testing. What the layout needs from a driver is glyph dimensions and
# somewhere to blit; the pixels are the driver's business and are covered on
# the hardware by selftest_display.
_ili9341 = types.ModuleType("ili9341")
for _index, _name in enumerate(
        ("BLACK", "WHITE", "GREY", "DIM", "RED", "GREEN", "AMBER", "BLUE")):
    setattr(_ili9341, _name, _index)


class _Glyphs:
    """Reports the real cell size for a scale, and renders nothing."""

    def __init__(self, scale=1):
        self.scale = scale
        self.width = 8 * scale
        self.height = 8 * scale

    def render(self, char, color, background):
        return None


_ili9341.Glyphs = _Glyphs
_ili9341.color565 = lambda r, g, b: 0
sys.modules.setdefault("ili9341", _ili9341)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "micropython"))

from pendant.screen import (DroScreen, AXES, STATUS_PITCH,  # noqa: E402
                            ROW_PITCH, STEP_ROWS)
from pendant.jog import STEP_SIZES  # noqa: E402
from pendant.screen import Zones  # noqa: E402

failures = []


def check(label, got, want):
    ok = got == want
    print("  {:<52} {}".format(label, "PASS" if ok else "FAIL"))
    if not ok:
        print("      got  {!r}\n      want {!r}".format(got, want))
        failures.append(label)


class FakeDisplay:
    """Records blits instead of driving a panel."""

    def __init__(self, width=320, height=240):
        self.width = width
        self.height = height
        self.blits = []
        self.rects = []
        self.fills = 0

    def fill(self, color):
        self.fills += 1
        self.blits = []
        self.rects = []

    def fill_rect(self, x, y, w, h, color):
        self.rects.append((x, y, w, h))

    def blit(self, buffer, x, y, w, h):
        self.blits.append((x, y, w, h))


def extent(display):
    """Bounding box of everything drawn, as (right, bottom)."""
    drawn = display.blits + display.rects
    right = max((x + w for x, _, w, _ in drawn), default=0)
    bottom = max((y + h for _, y, _, h in drawn), default=0)
    return right, bottom


print("layout on the fitted 320x240 panel")

display = FakeDisplay(320, 240)
screen = DroScreen(display)

# Coordinates the hardware was tuned against. The status block used to be at
# fixed y of 170/194/218; it is now anchored to the bottom edge, and on this
# panel that has to come out at exactly the same place or the change was not
# a refactor.
check("state line sits where it always did", screen._state.y, 170)
check("  mode line too", screen._mode.y, 194)
check("  and the link line", screen._link.y, 218)
check("position rows unchanged",
      [screen._positions[a].y for a in AXES], [20, 66, 112])

right, bottom = extent(display)
check("nothing is drawn past the right edge", right <= 320, True)
check("  or past the bottom", bottom <= 240, True)

# The mode line carries the commanded feed, so a field too narrow for the
# longest string it can produce does not merely look cramped - Field aligns
# then truncates from the right, so F15000 rendered as F150. The panel showing
# a different number to the one being sent is the failure worth a test.
worst = "{} {} {} F{:.0f}".format("X", 0.001, "QUEUE", 15000)
check("the widest mode line fits without truncation",
      len(worst) <= screen._mode.length, True)
check("  and without running off the panel",
      screen._mode.x + screen._mode.length * 16 <= 320, True)

print("\na taller panel")

# The case the split exists for. Status is anchored to the bottom, so extra
# height goes to the middle rather than stranding the status off-screen - which
# is what fixed coordinates would have done.
tall = FakeDisplay(320, 480)
tall_screen = DroScreen(tall)
check("status follows the bottom edge", tall_screen._link.y, 480 - 22)
check("  keeping its spacing", tall_screen._mode.y,
      tall_screen._link.y - STATUS_PITCH)
# Rows spread rather than keeping the short panel's pitch. The extra height is
# what turns each row from a readout into a touch target.
#
# Asserted in millimetres, not pixels. Pixels say nothing about whether a
# finger can hit it: this panel is 49.56 mm across 320 px, so a pixel is
# 0.155 mm, and the same count on a different panel would be a different
# target. A fingertip needs about 9 mm when used without looking, which a
# pendant is.
PX_PER_MM = 320 / 49.56
tall_rows = [tall_screen._positions[a].y for a in AXES]
tall_pitch = tall_rows[1] - tall_rows[0]
check("position rows spread to fill the extra height", tall_pitch > ROW_PITCH,
      True)
check("  giving each row a fingertip-sized target",
      tall_pitch / PX_PER_MM >= 9.0, True)
check("  and they stay evenly spaced", tall_rows[2] - tall_rows[1], tall_pitch)

right, bottom = extent(tall)
check("still nothing past the right edge", right <= 320, True)
check("  or the bottom", bottom <= 480, True)

print("\nstep zones")

# Every step must be reachable by touch once the physical step buttons are
# gone, and every target must select a step that exists. Drift either way is
# invisible: a missing target is a step the operator simply cannot choose.
laid_out = tuple(step for row in STEP_ROWS for step in row)
check("the grid covers every step size", sorted(laid_out), sorted(STEP_SIZES))

# Registered geometry has to match what was drawn, which is why the same code
# does both. Tapping the centre of a cell must select that cell's step.
for step in laid_out:
    zx, zy, zw, zh = tall_screen._zone_fields[step]
    check("  tapping the {} cell selects it".format(step),
          tall_screen.zones.hit(zx + zw // 2, zy + zh // 2), ("step", step))
    check("    and it clears a fingertip",
          min(zw, zh) / PX_PER_MM >= 9.0, True)

# Cells must tile: no gap that swallows a tap, and the last reaches the right
# edge rather than leaving a sliver belonging to nothing.
for row in STEP_ROWS:
    boxes = sorted(tall_screen._zone_fields[s] for s in row)
    check("  a row of {} reaches the right edge".format(len(row)),
          boxes[-1][0] + boxes[-1][2], 320)
    gaps = [boxes[i + 1][0] - (boxes[i][0] + boxes[i][2])
            for i in range(len(boxes) - 1)]
    check("    with no gaps between cells", gaps, [0] * len(gaps))

# Axis rows are targets too - that is what replaces the axis toggle button.
for index, axis in enumerate(AXES):
    check("  tapping the {} row selects it".format(axis),
          tall_screen.zones.hit(160, tall_rows[index] + 10), ("axis", axis))

# The short panel has no room, so it must not pretend: nothing drawn, and the
# physical buttons stay the only way to change step.
check("a short panel draws no step grid", screen._zones_shown, False)
check("  and registers no step zone", screen.zones.hit(160, 200), None)

# The selected axis carries a border like the step cells, and every row carries
# a dim one - which is the only thing on the panel saying the rows are tappable
# at all.
tall_screen.set_mode("Y", 0.1, True, 0.0)
check("every row has a border box, not just the selected one",
      sorted(tall_screen._axis_boxes), sorted(AXES))
selected_box = tall_screen._axis_boxes["Y"]
check("  and the border matches the row's touch target",
      tall_screen.zones.hit(selected_box[0] + 10, selected_box[1] + 10),
      ("axis", "Y"))

# A border overlapping the digits it surrounds would clip a column silently,
# the driver clamping rather than complaining.
for axis in AXES:
    bx, by, bw, bh = tall_screen._axis_boxes[axis]
    digits = tall_screen._positions[axis]
    check("  the {} border clears its own digits".format(axis),
          by + 2 <= digits.y and digits.y + 32 <= by + bh - 2, True)

print("\nzone arithmetic")

# Exercised directly, because the layout above only ever taps cell centres and
# these are the cases that bite at the edges.
zones = Zones()
zones.add("axis", 0, 20, 320, 46, "X")
zones.add("axis", 0, 66, 320, 46, "Y")

# Half-open bounds, so adjacent zones tile without overlapping and without a
# dead line between them that swallows a tap.
check("the boundary belongs to the lower zone", zones.hit(160, 66),
      ("axis", "Y"))
check("  and the one above ends just before it", zones.hit(160, 65),
      ("axis", "X"))
check("outside every zone hits nothing", zones.hit(160, 5), None)
check("  including past the width", zones.hit(400, 40), None)

# Later wins, matching what the operator sees: whatever is drawn on top is
# what they are aiming at.
stacked = Zones()
stacked.add("under", 0, 0, 200, 100, "background")
stacked.add("over", 50, 25, 100, 50, "button")
check("the zone registered last answers", stacked.hit(100, 50),
      ("over", "button"))
check("  while beside it the one beneath still does", stacked.hit(10, 50),
      ("under", "background"))

stacked.clear()
check("clearing removes every zone", stacked.hit(100, 50), None)

print("\nredraw")

# build() replaces the splash once the panel is proven, and used to be done by
# calling __init__ again with the SPI bus dug out of the screen object.
display.fills = 0
screen.splash("connecting...")
screen.build()
check("build repaints and restores the layout", display.fills, 2)
check("  and fields redraw after it", screen._state.y, 170)

print()
if failures:
    print("{} FAILED: {}".format(len(failures), ", ".join(failures)))
    sys.exit(1)
print("all screen layout tests passed")
