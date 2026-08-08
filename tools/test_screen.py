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

from pendant.screen import DroScreen, AXES, STATUS_PITCH  # noqa: E402

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
        self.fills = 0

    def fill(self, color):
        self.fills += 1
        self.blits = []

    def blit(self, buffer, x, y, w, h):
        self.blits.append((x, y, w, h))


def extent(display):
    """Bounding box of everything drawn, as (right, bottom)."""
    right = max((x + w for x, _, w, _ in display.blits), default=0)
    bottom = max((y + h for _, y, _, h in display.blits), default=0)
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
check("position rows stay at the top",
      [tall_screen._positions[a].y for a in AXES], [20, 66, 112])

right, bottom = extent(tall)
check("still nothing past the right edge", right <= 320, True)
check("  or the bottom", bottom <= 480, True)

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
