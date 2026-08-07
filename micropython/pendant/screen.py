"""DRO layout for the pendant.

Sits behind a small interface - `set_position`, `set_state`, `set_mode`,
`set_link` - so the panel choice stays a swap of the driver import rather than
a rewrite. Nothing above this module knows the display is an ILI9341.

Everything redraws per character and only where the character actually
changed. A DRO is mostly static: on a typical status frame two or three digits
move and the rest do not. Repainting whole lines at 10 Hz would be both slow
and visibly flickery, and would leave no headroom for anything else on the
core.
"""

try:
    from ili9341 import (ILI9341, Glyphs, color565,
                         BLACK, WHITE, GREY, DIM, RED, GREEN, AMBER, BLUE)
except ImportError:
    from pendant.ili9341 import (ILI9341, Glyphs, color565,
                                 BLACK, WHITE, GREY, DIM, RED, GREEN, AMBER,
                                 BLUE)

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
    """Three-axis DRO with machine state, selected axis and step size."""

    def __init__(self, spi, cs, dc, rst=None, backlight=None, rotation=90):
        self.display = ILI9341(spi, cs, dc, rst, backlight, rotation)
        self.display.fill(BLACK)

        big = Glyphs(scale=4)      # 32 px - the position readout
        medium = Glyphs(scale=3)   # 24 px - axis labels
        small = Glyphs(scale=2)    # 16 px - status lines
        self._big = big
        self._small = small

        # Layout for 320x240 landscape. Three position rows, status beneath.
        #
        # The width is tight and drives the sizes: nine digit cells at 32 px is
        # 288 of the 320 available, and nine is what "-1234.567" needs. Starting
        # the digits at x=30 leaves 26 px for the label, which fits a 24 px
        # glyph - so the label reads at three quarters of the digit height
        # rather than half, which matters because the highlighted axis is what
        # you glance at to know what the wheel will move.
        self._labels = {}
        self._positions = {}
        for row, axis in enumerate(AXES):
            y = 20 + row * 46
            label = Field(self.display, medium, 4, y + 4, 1, color=GREY)
            label.set(axis)
            self._labels[axis] = label
            self._positions[axis] = Field(self.display, big, 30, y, 9)

        self._state = Field(self.display, small, 8, 170, 10, color=GREEN)
        self._mode = Field(self.display, small, 8, 194, 20, color=WHITE)
        self._link = Field(self.display, small, 8, 218, 14, color=AMBER)

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
        self._mode.set("{} {}mm {} F{:.0f}".format(
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
        Field(self.display, self._small, 8, 100, len(message)).set(message)
