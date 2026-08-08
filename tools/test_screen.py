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
                            ROW_PITCH, STEP_ROWS, PAGE_ZONE_X)
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

# Two status rows now, not three. Axis, step and HALT/QUEUE left the third:
# the first two are shown by which row and cell is outlined, and the last has
# not changed since the jog tuning settled.
check("the link line stays anchored to the bottom", screen._link.y, 218)
check("  with the state line one pitch above", screen._state.y,
      218 - STATUS_PITCH)
check("  and the feed sharing the state row", screen._mode.y, screen._state.y)
# Digits are centred in their row box, not pinned to its top. They used to sit
# 4 px below the top edge and 27 px above the bottom, which read as fine while
# the row was only a readout and read as broken the moment it gained a border.
for _index, _axis in enumerate(AXES):
    _top = 20 + _index * ROW_PITCH
    _above = screen._positions[_axis].y - _top
    _below = _top + ROW_PITCH - (screen._positions[_axis].y + 32)
    check("  {} digits are centred in their row".format(_axis),
          abs(_above - _below) <= 1, True)

right, bottom = extent(display)
check("nothing is drawn past the right edge", right <= 320, True)
check("  or past the bottom", bottom <= 240, True)

# The mode line carries the commanded feed, so a field too narrow for the
# longest string it can produce does not merely look cramped - Field aligns
# then truncates from the right, so F15000 rendered as F150. The panel showing
# a different number to the one being sent is the failure worth a test.
worst = "F{:.0f}".format(15000)
check("the widest feed fits without truncation",
      len(worst) <= screen._mode.length, True)
check("  and without running off the panel",
      screen._mode.x + screen._mode.length * 16 <= 320, True)

# The status row has three things competing for one line. Any two meeting is
# a silent clip, the driver clamping rather than raising.
check("  the state text clears the feed",
      screen._state.x + screen._state.length * 16 <= screen._mode.x, True)
check("  and the feed clears the corner",
      screen._mode.x + screen._mode.length * 16 <= PAGE_ZONE_X, True)
check("  and the link line clears it too",
      screen._link.x + screen._link.length * 16 <= PAGE_ZONE_X, True)

print("\na taller panel")

# The case the split exists for. Status is anchored to the bottom, so extra
# height goes to the middle rather than stranding the status off-screen - which
# is what fixed coordinates would have done.
tall = FakeDisplay(320, 480)
tall_screen = DroScreen(tall)
# The status text sits inside the bottom band rather than being measured from
# the panel edge, so it moves with the band instead of being stranded by it.
check("status sits inside the bottom band", 400 <= tall_screen._state.y < 480,
      True)
check("  with the link line below it", tall_screen._link.y >
      tall_screen._state.y, True)
check("  and both inside the panel",
      tall_screen._link.y + 24 <= 480, True)
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
# Within a pixel: the last row absorbs the rounding left by integer division
# so the boxes reach the grid exactly, which shifts its centred digits by one.
check("  and they stay evenly spaced",
      abs((tall_rows[2] - tall_rows[1]) - tall_pitch) <= 2, True)

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

# The regions have to tile. Positioning two of them independently from
# opposite ends is what put the corner inside the 1.0 cell: the grid was
# measured downward from the status text and the corner upward from the panel
# edge, so changing the status row count moved one and not the other.
last_box = tall_screen._axis_boxes[AXES[-1]]
first_cell = min(tall_screen._zone_fields.values(), key=lambda b: b[1])
check("  the position rows meet the step grid with no gap",
      first_cell[1] - (last_box[1] + last_box[3]), 0)

grid_bottom = max(y + h for _, y, _, h in tall_screen._zone_fields.values())
page = [z for z in tall_screen.zones._zones if z[0] == "page"][0]
page_top, page_bottom = page[2], page[4]
check("  and the grid clears the corner", grid_bottom <= page_top, True)
check("    with the corner clear of every cell",
      all(page[1] >= zx + zw or page_top >= zy + zh
          for zx, zy, zw, zh in tall_screen._zone_fields.values()), True)

# Lining up with the link text is what makes the corner look deliberate
# rather than floating.
# The corner fills the band's full height rather than floating inside it,
# which is what makes the bottom of the panel read as finished.
check("  the corner fills the band", page_bottom, 480)
check("    and starts where the grid ends", page_top, grid_bottom)

# The corner is reserved whether or not anything acts on it yet, so whatever
# lands there later inherits settled geometry instead of being retrofitted
# around the status text.
corner = tall_screen.zones.hit(280, 480 - 40)
check("  the corner is a registered target", corner, ("page", "next"))
check("    and clears a fingertip in both directions",
      min(320 - PAGE_ZONE_X, 68) / PX_PER_MM >= 9.0, True)
check("    without covering the status text",
      max(tall_screen._link.x + tall_screen._link.length * 16,
          tall_screen._mode.x + tall_screen._mode.length * 16) <= PAGE_ZONE_X,
      True)

# The short panel has no room, so it must not pretend: nothing drawn, and the
# physical buttons stay the only way to change step.
check("a short panel draws no step grid", screen._zones_shown, False)
check("  and registers no step zone", screen.zones.hit(160, 200), None)

# The selected axis carries a border like the step cells, and every row carries
# a dim one - which is the only thing on the panel saying the rows are tappable
# at all.
tall_screen.set_mode("Y", 0.1, 0.0)
check("every row has a border box, not just the selected one",
      sorted(tall_screen._axis_boxes), sorted(AXES))
selected_box = tall_screen._axis_boxes["Y"]
check("  and the border matches the row's touch target",
      tall_screen.zones.hit(selected_box[0] + 10, selected_box[1] + 10),
      ("axis", "Y"))

# A border overlapping the digits it surrounds would clip a column silently,
# the driver clamping rather than complaining. Vertically the digits must be
# centred, and horizontally they must clear the right edge - nine 32 px cells
# is 288 of 320, so the fit is exact to within a few pixels.
for axis in AXES:
    bx, by, bw, bh = tall_screen._axis_boxes[axis]
    digits = tall_screen._positions[axis]
    above = digits.y - by
    below = by + bh - (digits.y + 32)
    check("  the {} digits are centred in their border".format(axis),
          abs(above - below) <= 1, True)
    check("    with the border clear of them", above >= 2 and below >= 2, True)
    check("    and clear of the right edge",
          digits.x + digits.length * 32 <= bx + bw - 2, True)
    label = tall_screen._labels[axis]
    check("    the label centred too",
          abs((label.y - by) - (by + bh - (label.y + 24))) <= 1, True)

# Nothing may overhang the panel. The status text grew to the larger glyph when
# the band did and the corner label deliberately did not - "PAGE" at 24 px is
# 96 px inside an 80 px corner, which the driver clips silently rather than
# raising, so it would have shown as a truncated word.
for _name, _field in (("state", tall_screen._state),
                      ("link", tall_screen._link),
                      ("feed", tall_screen._mode)):
    check("  the {} field clears the corner".format(_name),
          _field.x + _field.length * _field.glyphs.width <= PAGE_ZONE_X, True)

_r, _b = extent(tall)
check("  and nothing at all is drawn past the panel",
      _r <= 320 and _b <= 480, True)

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
check("  and fields redraw after it", screen._state.y, 218 - STATUS_PITCH)

print()
if failures:
    print("{} FAILED: {}".format(len(failures), ", ".join(failures)))
    sys.exit(1)
print("all screen layout tests passed")
