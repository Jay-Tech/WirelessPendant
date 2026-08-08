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

# Step sizes as touch targets, laid out row by row.
#
# Must cover jog.STEP_SIZES exactly - a step reachable by button but not by
# touch is one the operator cannot select once the buttons are gone, and a
# touch target for a step that does not exist selects nothing. Asserted in the
# tests rather than trusted.
#
# Three then two rather than five across. The panel is 49.56 mm over 320 px, so
# five across is 9.9 mm per target - a fingertip, with nothing spare - and this
# is used without looking. Three across is 16.5 mm.
STEP_ROWS = ((0.001, 0.01, 0.1), (0.5, 1.0))

ZONE_ROW_H = 80
ZONE_GAP = 8
ZONE_MARGIN = 16

# Below this much free height the step grid is left out entirely and the
# physical step buttons remain the only way to change it. That is the 320x240
# panel, where the rows and the status block already meet.
MIN_ZONE_SPACE = ZONE_ROW_H * 2 + ZONE_GAP + ZONE_MARGIN * 2


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
        self._small = Glyphs(scale=2)    # 16 px - status lines and zone labels

        # Registered by whatever draws a target, so the geometry lives in one
        # place. A hit box that has drifted from its label is invisible: the
        # screen simply ignores you.
        self.zones = Zones()
        self._step = None
        self._axis = None
        self._axis_boxes = {}
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

        # A tall panel earns a step grid; a short one gives its height to the
        # position rows and keeps the physical step buttons.
        rows_min = TOP_MARGIN + len(AXES) * ROW_PITCH
        self._zones_shown = (state_y - rows_min) >= MIN_ZONE_SPACE

        if self._zones_shown:
            zones_top = state_y - ZONE_MARGIN - (ZONE_ROW_H * 2 + ZONE_GAP)
            pitch = (zones_top - TOP_MARGIN - ZONE_MARGIN) // len(AXES)
        else:
            zones_top = None
            pitch = ROW_PITCH

        self.zones.clear()
        self._axis_boxes = {}
        self._axis = None
        self._labels = {}
        self._positions = {}
        for row, axis in enumerate(AXES):
            y = TOP_MARGIN + row * pitch
            label = Field(self.display, self._medium, 4, y + 4, 1, color=GREY)
            label.set(axis)
            self._labels[axis] = label
            self._positions[axis] = Field(self.display, self._big, DIGITS_X, y,
                                          DIGITS)
            # The whole row is the target, not just the label. It is already
            # the thing that highlights to show what the wheel will move, so
            # tapping it to choose is the same gesture read backwards - and a
            # full-width row is the largest target the panel can offer.
            box = (0, y - 4, self.display.width, pitch)
            self._axis_boxes[axis] = box
            self.zones.add("axis", box[0], box[1], box[2], box[3], axis)
            if self._zones_shown:
                # Outlined only where the rows are actually targets. On a panel
                # too short for the step grid the buttons are still doing this
                # job, and a border promising a tap that is not read is worse
                # than no border.
                self._draw_border(box, False)

        self._zone_fields = {}
        if self._zones_shown:
            self._build_step_zones(zones_top)

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

    def _build_step_zones(self, top):
        """Draw the step grid and register each cell as it is drawn."""
        width = self.display.width
        for row_index, row in enumerate(STEP_ROWS):
            y = top + row_index * (ZONE_ROW_H + ZONE_GAP)
            # The last cell in a row absorbs the rounding, so the grid reaches
            # the right edge exactly instead of leaving a sliver that belongs
            # to nothing and swallows taps.
            cell = width // len(row)
            for index, step in enumerate(row):
                x = index * cell
                w = (width - x) if index == len(row) - 1 else cell
                self._draw_zone(x, y, w, ZONE_ROW_H, step, False)
                self.zones.add("step", x, y, w, ZONE_ROW_H, step)
                self._zone_fields[step] = (x, y, w, ZONE_ROW_H)

    def _draw_border(self, box, selected):
        """Outline a touch target. Dim means tappable, amber means selected.

        Every target carries one, not just the active one. A border that
        appears only on selection says nothing about what else can be tapped,
        and on a panel with no other affordance that is the whole of the
        discoverability.

        An outline rather than a filled block: a solid highlight at this size
        is a lot of lit pixels beside a position readout that has to stay
        legible in a lit shop.
        """
        x, y, w, h = box
        edge = AMBER if selected else DIM
        self.display.fill_rect(x, y, w, 2, edge)
        self.display.fill_rect(x, y + h - 2, w, 2, edge)
        self.display.fill_rect(x, y, 2, h, edge)
        self.display.fill_rect(x + w - 2, y, 2, h, edge)

    def _draw_zone(self, x, y, w, h, step, selected):
        """One step cell: a border, and its value centred."""
        self.display.fill_rect(x, y, w, h, BLACK)
        self._draw_border((x, y, w, h), selected)

        text = "{:g}".format(step)
        glyph_w = self._small.width
        text_x = x + (w - len(text) * glyph_w) // 2
        text_y = y + (h - self._small.height) // 2
        field = Field(self.display, self._small, text_x, text_y, len(text),
                      color=WHITE if selected else GREY)
        field.set(text)

    def set_step_highlight(self, step):
        """Mark which step cell is selected, repainting only what changed."""
        if not self._zones_shown or step == self._step:
            return
        for value, box in self._zone_fields.items():
            was = value == self._step
            now = value == step
            if was or now:
                self._draw_zone(box[0], box[1], box[2], box[3], value, now)
        self._step = step

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
        self.set_step_highlight(step)
        # Highlight the selected axis label so the operator can see what the
        # wheel will move without reading the smaller mode line.
        for name, field in self._labels.items():
            field.set(name, AMBER if name == axis else GREY)

        # And move the border, repainting only the two rows whose state
        # changed rather than all of them - the same reason every other field
        # here tracks what it last drew.
        if self._zones_shown and axis != self._axis:
            for name, box in self._axis_boxes.items():
                if name in (axis, self._axis):
                    self._draw_border(box, name == axis)
            self._axis = axis

    def set_link(self, connected):
        self._link.set("link up" if connected else "LINK DOWN",
                       GREEN if connected else RED)

    def splash(self, message):
        self.display.fill(BLACK)
        Field(self.display, self._small, 8, self.display.height // 2,
              len(message)).set(message)


class Zones:
    """Rectangular hit targets, resolved newest-first.

    Held as data rather than as code so the layout owns the geometry and this
    owns nothing but the arithmetic. Anything that draws a target registers the
    same rectangle it drew, which is what stops the two drifting apart - a
    touch target that has quietly moved away from its label is invisible until
    someone is standing at the machine wondering why the screen ignores them.
    """

    def __init__(self):
        self._zones = []

    def add(self, name, x, y, w, h, value=None):
        self._zones.append((name, x, y, x + w, y + h, value))

    def clear(self):
        self._zones = []

    def hit(self, x, y):
        """(name, value) for the zone containing the point, or None.

        Later registrations win, so a zone drawn on top of another is the one
        that answers - matching what the operator can see.
        """
        for name, x0, y0, x1, y1, value in reversed(self._zones):
            if x0 <= x < x1 and y0 <= y < y1:
                return name, value
        return None
