"""DRO layout for the pendant.

Sits behind a small interface - `set_position`, `set_state`, `set_mode`,
`set_link` - so nothing above this module knows which panel is fitted.

The display is passed in rather than constructed here. That is the difference
between the panel being a swap and being a rewrite: this module needs only
`width`, `height`, `fill` and `blit` from it, so any driver offering those can
be substituted without touching the layout, and a new panel can be brought up
alongside the old one rather than replacing it.

Everything redraws per character and only where the character actually
changed. A DRO is mostly static: on a typical status frame two or three digits
move and the rest do not. Repainting whole lines at 10 Hz would be both slow
and visibly flickery, and would leave no headroom for anything else on the
core.
"""

try:
    from ili9341 import (Glyphs, BLACK, WHITE, GREY, DIM, RED, GREEN, AMBER,
                         BLUE)
except ImportError:
    from pendant.ili9341 import (Glyphs, BLACK, WHITE, GREY, DIM, RED, GREEN,
                                 AMBER, BLUE)

AXES = ("X", "Y", "Z")

# grblHAL states worth colouring. Anything unlisted stays white rather than
# being guessed at, so an unfamiliar state is visibly unfamiliar.
STATE_COLORS = {
    "Idle": GREEN,
    "Run": GREEN,
    "Jog": BLUE,
    "Hold": AMBER,
    "Door": AMBER,
    "Alarm": RED,
    "Home": BLUE,
    "Check": GREY,
    "Sleep": DIM,
}

# Vertical pitch of a position row, and of a status line. Fixed rather than
# derived: the digits are 32 px and the status glyphs 16, so these are the
# glyph sizes plus breathing room, not a fraction of the panel.
ROW_PITCH = 46
STATUS_PITCH = 24

# Digits start here, leaving room for the axis label to its left. Nine cells of
# 32 px is 288 of a 320 px panel - nine being what "-1234.567" needs - so the
# 30 px remaining is what the label has to fit in, and a 24 px glyph does. The
# label reads at three quarters of the digit height rather than half, which
# matters because the highlighted axis is what you glance at to know what the
# wheel will move.
DIGITS_X = 30
DIGITS = 9
TOP_MARGIN = 20


class Field:
    """Fixed-position text, repainting only the characters that changed."""

    def __init__(self, display, glyphs, x, y, length,
                 color=WHITE, background=BLACK):
        self.display = display
        self.glyphs = glyphs
        self.x = x
        self.y = y
        self.length = length
        self.color = color
        self.background = background
        self._shown = " " * length

    def set(self, text, color=None):
        if color is not None and color != self.color:
            # A colour change invalidates every cell, not just changed ones.
            self.color = color
            self._shown = "\x00" * self.length

        text = "{:>{}}".format(text, self.length)[:self.length]
        width = self.glyphs.width
        for index in range(self.length):
            char = text[index]
            if char == self._shown[index]:
                continue
            cell = self.glyphs.render(char, self.color, self.background)
            self.display.blit(cell, self.x + index * width, self.y,
                              width, self.glyphs.height)
        self._shown = text

    def invalidate(self):
        self._shown = "\x00" * self.length


class DroScreen:
    """Three-axis DRO with machine state, selected axis and step size.

    Takes a constructed display. Anything offering width, height, fill and
    blit will do.
    """

    def __init__(self, display):
        self.display = display
        self._big = Glyphs(scale=4)      # 32 px - the position readout
        self._medium = Glyphs(scale=3)   # 24 px - axis labels
        self._small = Glyphs(scale=2)    # 16 px - status lines
        self.build()

    def build(self):
        """Paint the layout and reset every field.

        Separate from construction so the splash can be replaced once the panel
        is known good, without re-running __init__ and without the caller
        needing to know how the display was built. That used to mean reaching
        back through the screen for its SPI bus and pin numbers.
        """
        self.display.fill(BLACK)

        # Status lines are anchored to the bottom and the position rows to the
        # top, so a taller panel gives its extra height to the middle rather
        # than stranding the status off the bottom edge.
        link_y = self.display.height - 22
        mode_y = link_y - STATUS_PITCH
        state_y = mode_y - STATUS_PITCH

        self._labels = {}
        self._positions = {}
        for row, axis in enumerate(AXES):
            y = TOP_MARGIN + row * ROW_PITCH
            label = Field(self.display, self._medium, 4, y + 4, 1, color=GREY)
            label.set(axis)
            self._labels[axis] = label
            self._positions[axis] = Field(self.display, self._big, DIGITS_X, y,
                                          DIGITS)

        # The mode line is the widest thing on the panel and starts hard against
        # the left edge, because it needs every cell: 16 px glyphs give exactly
        # width/16 of them, and at 320 px that is 20 with nothing to spare.
        # Indented by 8 like the others it ran 8 px past the right edge, and the
        # driver clamps rather than complains, so the last character was quietly
        # losing its right half.
        cells = self.display.width // self._small.width
        self._state = Field(self.display, self._small, 8, state_y, 10,
                            color=GREEN)
        self._mode = Field(self.display, self._small, 0, mode_y, cells,
                           color=WHITE)
        self._link = Field(self.display, self._small, 8, link_y, 14,
                           color=AMBER)

        self._last_position = {}
        self.set_position((0.0, 0.0, 0.0))
        self.set_state("?")
        self.set_mode("?", 0.0, True, 0.0)
        self.set_link(False)

    def set_position(self, values):
        for axis, value in zip(AXES, values):
            if self._last_position.get(axis) == value:
                continue
            self._last_position[axis] = value
            self._positions[axis].set("{:.3f}".format(value))

    def set_state(self, state):
        self._state.set(state, STATE_COLORS.get(state, WHITE))

    def set_mode(self, axis, step, halt_on_stop=True, feed=0.0):
        # HALT: stopping the wheel flushes queued motion. QUEUE: every detent
        # is honoured and the machine finishes what it was given. Shown because
        # the two feel different enough that guessing which is active while
        # standing at the machine is worse than the space it costs.
        #
        # Feed is shown because it is the thing that varies with how you turn:
        # distance per detent never changes, so a rising number here is the only
        # visible sign that winding faster is doing anything.
        # No "mm" suffix. The step is always millimetres, and the longest
        # string this can produce - "X 0.001 QUEUE F15000" - is exactly the 20
        # cells a 320 px panel has. With the suffix it was 22, and Field
        # truncates from the right after aligning, so the feed lost its last
        # two digits: F15000 displayed as F150. A readout that silently shows a
        # different number to the one commanded is worse than a missing unit.
        self._mode.set("{} {} {} F{:.0f}".format(
            axis, step, "HALT" if halt_on_stop else "QUEUE", feed))
        # Highlight the selected axis label so the operator can see what the
        # wheel will move without reading the smaller mode line.
        for name, field in self._labels.items():
            field.set(name, AMBER if name == axis else GREY)

    def set_link(self, connected):
        self._link.set("link up" if connected else "LINK DOWN",
                       GREEN if connected else RED)

    def splash(self, message):
        self.display.fill(BLACK)
        Field(self.display, self._small, 8, self.display.height // 2,
              len(message)).set(message)
