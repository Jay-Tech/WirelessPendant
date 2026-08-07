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
        small = Glyphs(scale=2)    # 16 px - labels and status
        self._big = big
        self._small = small

        # Layout for 320x240 landscape. Three position rows down the left,
        # status stacked on the right.
        self._labels = {}
        self._positions = {}
        for row, axis in enumerate(AXES):
            y = 20 + row * 46
            label = Field(self.display, small, 8, y + 8, 1, color=GREY)
            label.set(axis)
            self._labels[axis] = label
            # 9 cells fits "-9999.999" without ever reflowing the layout.
            self._positions[axis] = Field(self.display, big, 32, y, 9)

        self._state = Field(self.display, small, 8, 170, 10, color=GREEN)
        self._mode = Field(self.display, small, 8, 194, 14, color=WHITE)
        self._link = Field(self.display, small, 8, 218, 14, color=AMBER)

        self._last_position = {}
        self.set_position((0.0, 0.0, 0.0))
        self.set_state("?")
        self.set_mode("?", 0.0)
        self.set_link(False)

    def set_position(self, values):
        for axis, value in zip(AXES, values):
            if self._last_position.get(axis) == value:
                continue
            self._last_position[axis] = value
            self._positions[axis].set("{:.3f}".format(value))

    def set_state(self, state):
        self._state.set(state, STATE_COLORS.get(state, WHITE))

    def set_mode(self, axis, step):
        self._mode.set("{} {}mm".format(axis, step))
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
