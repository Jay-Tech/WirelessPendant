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
# Two pixels clear of the row border on the right. Nine cells of 32 px is 288,
# and the panel is 320 with a 2 px border each side, so the fit is exact to
# within a few pixels: starting at 28 puts the last digit at 316 against a
# border at 318. Starting at 30 put them flush against it.
DIGITS_X = 28
DIGITS = 9
TOP_MARGIN = 20

# Height of the hold-progress bar along the top edge. It sits inside the top
# margin, so it costs no layout - the rows already start below it.
HOLD_BAR_H = 6

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

# The probe page. Each entry is (operation, label), and every one is
# hold-to-fire: these drive the tool at the work, so a tap must not start one.
#
# Only operations that are "position the tool, then probe" - which is the part
# the pendant is for. Centre finding stays on the sender, where the bore or
# boss selection that decides what it does is visible.
PROBE_OPS = (
    ("z", "PROBE Z"),
    ("corner", "PROBE CORNER"),
    ("tlr", "TOOL REF"),
)
PROBE_ROW_H = 92

# The probe rows start below the hold bar and the page heading, not at
# TOP_MARGIN. At 20 the heading sat under the hold bar and the first row's
# border ran through it.
PROBE_TOP = 32

# Where a two-line target's heading sits, and how far its qualifier clears the
# row's bottom border. The qualifier goes against the border rather than under
# the heading: packed together in the middle they read as one wrapped line.
PROBE_LABEL_TOP = 16
PROBE_DETAIL_GAP = 8

# The bottom band: status text on the left, the paging corner on the right.
# Sized to match a step-grid row, so the panel reads as one stack of equal
# bands rather than a layout with an offcut at the bottom.
BOTTOM_BAND_H = 80

# Below this the panel gets neither a step grid nor a bottom band - there is
# only room for the position rows and two lines of status. That is the 320x240
# panel, where the physical buttons are still doing the selecting.
TALL_PANEL = 400

# Where the corner starts. The status text has to fit to its left: at the
# larger glyph "LINK DOWN" is nine cells of 24 px, which is 216, and this
# leaves 232.
PAGE_ZONE_X = 240

# Cells for the status fields, sized to what they actually hold. "Alarm" and
# "Check" are the longest states, "LINK DOWN" the longest link text, "F15000"
# the longest feed. State and feed share a row and must not meet, and neither
# may reach the corner.
STATE_CELLS = 5
LINK_CELLS = 9
FEED_CELLS = 6

# Pitch between the two status lines when the band is tall enough for the
# larger glyph. The smaller STATUS_PITCH would overlap 24 px text.
STATUS_PITCH_LARGE = 36

# Grid rows tile against each other and against the band below, the same way
# the position rows tile against the grid. The gap that used to separate them
# was the last thing on the panel that floated.
ZONE_ROW_H = 80


class Field:
    """Fixed-position text, repainting only the characters that changed."""

    def __init__(self, display, glyphs, x, y, length,
                 color=WHITE, background=BLACK, align="right"):
        self.display = display
        self.glyphs = glyphs
        self.x = x
        self.y = y
        self.length = length
        self.color = color
        self.background = background
        # Right by default, because most fields here hold numbers and a number
        # that shifts sideways as it gains a digit is hard to read at a glance.
        # Words want the opposite: left-aligned they start in the same place
        # whatever their length, which is what makes a changing machine state
        # readable without looking directly at it.
        self.align = align
        self._shown = " " * length

    def set(self, text, color=None):
        if color is not None and color != self.color:
            # A colour change invalidates every cell, not just changed ones.
            self.color = color
            self._shown = "\x00" * self.length

        if self.align == "left":
            text = "{:<{}}".format(text, self.length)[:self.length]
        else:
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
        self._hold_width = 0
        self._step = None
        # Which page is showing. The DRO is page 0 and the one the pendant
        # returns to, because a pendant left on a menu is a pendant that does
        # not show where the machine is.
        self.page = 0
        self._probe_corner = "?"
        self._probe_busy = False
        self._probe_detail = None

        # The last values the pendant pushed, kept whatever page is showing.
        #
        # The DRO setters are called from the refresh loop regardless of page,
        # and their fields hold DRO coordinates - so on the probe page they
        # drew digits over it. Holding the values here means the page can
        # ignore them and still show them the moment it comes back, rather
        # than reading zero until the next status frame.
        self._latest = {"position": None, "state": None, "link": None,
                        "mode": None}
        self._axis = None
        self._axis_boxes = {}
        self.build()

    def show_page(self, page):
        """Switch pages and repaint. Page 0 is the DRO, page 1 the probe menu."""
        if page == self.page:
            return
        self.page = page
        self.build()

    def set_probe_state(self, corner, busy):
        """The sender's corner selection and whether a cycle is running.

        Shown rather than chosen. Which corner a probe uses lives in the sender
        and there is no reason for a second copy here - but firing a cycle
        whose target is only visible on a screen behind you is exactly the
        thing that made the shared probe parameters unsafe, so it has to be
        readable at the machine.
        """
        corner_changed = corner != self._probe_corner
        busy_changed = busy != self._probe_busy
        if not corner_changed and not busy_changed:
            return

        self._probe_corner = corner
        self._probe_busy = busy
        if self.page != 1:
            return

        # A corner change repaints the corner, not the panel. build() begins
        # with a full fill, so rebuilding for nine characters flashed the whole
        # screen black - which on a page whose targets start a probe reads as
        # something having gone wrong.
        if corner_changed and not busy_changed and self._probe_detail:
            self._probe_detail.length = self._fits(corner, self._small)
            self._probe_detail.invalidate()
            self._probe_detail.set(corner)
            return

        # Busy changes which targets are armed and how every row is drawn, so
        # that one does rebuild. It happens when a cycle starts or ends, where
        # a repaint is expected rather than startling.
        self.build()

    def build(self):
        """Paint the layout and reset every field.

        Separate from construction so the splash can be replaced once the panel
        is known good, without re-running __init__ and without the caller
        needing to know how the display was built. That used to mean reaching
        back through the screen for its SPI bus and pin numbers.
        """
        self.display.fill(BLACK)
        self._hold_width = 0
        self.zones.clear()

        if self.page == 1:
            self._build_probe_page()
            return

        # Laid out bottom-up, every region flush against the one below it.
        #
        # Positioning two regions independently from opposite ends is what
        # broke this once: the grid was measured downward from the status text
        # and the corner upward from the panel edge, so changing the number of
        # status rows moved one and not the other, and drove the corner through
        # the 1.0 cell. Chaining them means a change to any one shifts the rest
        # rather than colliding with them.
        #
        # Nothing floats. The gaps that used to sit between the position rows
        # and the grid, between the two grid rows, and under the corner were
        # each a different constant doing the same job badly - and the sum of
        # them read as the bottom of the panel being unfinished.
        band_h = BOTTOM_BAND_H if self.display.height >= TALL_PANEL else 0
        band_top = self.display.height - band_h
        zones_top = band_top - ZONE_ROW_H * 2

        # Room only counts if the grid still leaves the position rows their
        # minimum pitch. Below that the whole bottom half is dropped and the
        # physical buttons remain the only way to change step.
        self._zones_shown = (band_h > 0 and
                             zones_top >= TOP_MARGIN + len(AXES) * ROW_PITCH)

        if self._zones_shown:
            # Rows tile the whole span down to the grid, so the two meet
            # instead of leaving a band of empty panel between the last border
            # and the first cell.
            pitch = (zones_top - TOP_MARGIN) // len(AXES)
            # A taller band earns larger status text. It is the part read at a
            # glance from arm's length, and on the short panel there was never
            # room to make it any bigger.
            status = self._medium
            status_pitch = STATUS_PITCH_LARGE
            state_y = band_top + 8
        else:
            zones_top = None
            pitch = ROW_PITCH
            status = self._small
            status_pitch = STATUS_PITCH
            state_y = self.display.height - 22 - STATUS_PITCH
        link_y = state_y + status_pitch

        self.zones.clear()
        self._hold_width = 0
        self._axis_boxes = {}
        self._axis = None
        self._labels = {}
        self._positions = {}
        for row, axis in enumerate(AXES):
            # The box comes first and its contents are centred inside it,
            # rather than the text being placed and a box drawn near it. Done
            # the other way round the digits sat 4 px below the top edge and
            # 27 px above the bottom - fine while the row was only a readout,
            # and obviously wrong the moment it gained a border to sit inside.
            top = TOP_MARGIN + row * pitch
            # Integer division leaves up to two pixels over; the last row takes
            # them, so the bottom border lands on the grid rather than a
            # hairline above it.
            height = pitch
            if self._zones_shown and row == len(AXES) - 1:
                height = zones_top - top
            box = (0, top, self.display.width, height)

            label = Field(self.display, self._medium, 4,
                          top + (height - self._medium.height) // 2, 1,
                          color=GREY)
            label.set(axis)
            self._labels[axis] = label
            self._positions[axis] = Field(
                self.display, self._big, DIGITS_X,
                top + (height - self._big.height) // 2, DIGITS)

            # The whole row is the target, not just the label. It is already
            # the thing that highlights to show what the wheel will move, so
            # tapping it to choose is the same gesture read backwards - and a
            # full-width row is the largest target the panel can offer.
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
        self._state = Field(self.display, status, 8, state_y,
                            STATE_CELLS, color=GREEN, align="left")
        self._link = Field(self.display, status, 8, link_y, LINK_CELLS,
                           color=AMBER, align="left")

        # Feed shares the state row, right of the state text and left of the
        # corner, right-aligned so the digits do not shuffle as it changes.
        #
        # Kept at the smaller glyph even when the status text grows. At the
        # larger one, "Alarm" and "F15000" together need eleven cells and the
        # space left of the corner holds nine - the state would have had to
        # shrink to fit, and it is the more glanced of the two.
        feed_w = FEED_CELLS * self._small.width
        self._mode = Field(self.display, self._small,
                           PAGE_ZONE_X - feed_w - 8,
                           state_y + (status.height - self._small.height) // 2,
                           FEED_CELLS, color=WHITE)

        # The corner. Registered whether or not anything acts on it yet, so the
        # geometry is settled and reserved rather than being retrofitted around
        # whatever lands here later.
        if self._zones_shown:
            page_box = (PAGE_ZONE_X, band_top,
                        self.display.width - PAGE_ZONE_X, band_h)
            self._draw_border(page_box, False)
            self.zones.add("page", page_box[0], page_box[1],
                           page_box[2], page_box[3], "next")
            label = "PAGE"
            # Small glyph, unlike the status text beside it. "PAGE" at the
            # larger one is 96 px inside an 80 px corner, so it overhung the
            # panel edge - and the driver clips rather than complaining, so it
            # would have shown as a truncated word rather than an error.
            text_y = band_top + (band_h - self._small.height) // 2
            Field(self.display, self._small,
                  page_box[0] + (page_box[2]
                                 - len(label) * self._small.width) // 2,
                  text_y, len(label), color=GREY).set(label)

        # Repainted from what was last pushed, not from placeholders. Coming
        # back from the probe page to a DRO reading zero, until the next status
        # frame overwrote it, looked like the machine had lost its position.
        self._last_position = {}
        latest = self._latest
        self.set_position(latest["position"] or (0.0, 0.0, 0.0))
        self.set_state(latest["state"] or "?")
        mode = latest["mode"] or ("?", 0.0, 0.0)
        self.set_mode(mode[0], mode[1], mode[2])
        self.set_link(bool(latest["link"]))

    def _build_probe_page(self):
        """The probe menu: one full-width target per operation, plus a way back.

        Full width and 92 px tall - 14 mm - because these are the targets you
        reach for while looking at a stylus rather than at the screen, and they
        are the only ones on this pendant that move a tool at a workpiece.

        Every label here is sized against the panel rather than written and
        hoped for. At 24 px a character is 24 px wide, so thirteen of them fill
        a 320 px screen: "PROBE CORNER FrontLeft" on one line came to 528, and
        the driver clamps rather than complaining, so it rendered as a torn
        diagonal instead of an error.
        """
        width = self.display.width
        y = PROBE_TOP

        Field(self.display, self._small, 8, 12, 11, color=GREY,
              align="left").set("HOLD TO RUN")

        self._probe_fields = {}
        for operation, label in PROBE_OPS:
            box = (0, y, width, PROBE_ROW_H)
            self._draw_border(box, False)
            colour = DIM if self._probe_busy else WHITE

            # The corner goes on a second line at the smaller glyph. Appended
            # to the label it was more than twice the panel width.
            #
            # It sits against the bottom border rather than under the label,
            # so the row reads as a heading with a qualifier beneath it. Packed
            # together in the middle they looked like one wrapped line.
            detail = self._probe_corner if operation == "corner" else None
            top = (y + PROBE_LABEL_TOP if detail
                   else y + (PROBE_ROW_H - self._medium.height) // 2)

            field = Field(self.display, self._medium, 12, top,
                          self._fits(label, self._medium), color=colour,
                          align="left")
            field.set(label)
            self._probe_fields[operation] = field

            if detail:
                detail_y = y + PROBE_ROW_H - 2 - PROBE_DETAIL_GAP                     - self._small.height
                self._probe_detail = Field(
                    self.display, self._small, 14, detail_y,
                    self._fits(self._probe_corner, self._small),
                    color=AMBER if not self._probe_busy else DIM,
                    align="left")
                self._probe_detail.set(detail)

            # Nothing is armed while a cycle is running. A second probe started
            # into a moving machine is the failure worth refusing outright.
            if not self._probe_busy:
                self.zones.add("probe", 0, y, width, PROBE_ROW_H, operation)
            y += PROBE_ROW_H

        status = "PROBING..." if self._probe_busy else "tap DRO to go back"
        Field(self.display, self._small, 8, y + 10,
              self._fits(status, self._small),
              color=AMBER if self._probe_busy else GREY,
              align="left").set(status)

        # The way back, in the same corner the way here was. A menu with no
        # visible exit is one the operator power-cycles out of.
        back_h = BOTTOM_BAND_H
        back_y = self.display.height - back_h
        back_box = (PAGE_ZONE_X, back_y, width - PAGE_ZONE_X, back_h)
        self._draw_border(back_box, False)
        self.zones.add("page", back_box[0], back_box[1], back_box[2],
                       back_box[3], "dro")
        label = "DRO"
        Field(self.display, self._small,
              back_box[0] + (back_box[2] - len(label) * self._small.width) // 2,
              back_y + (back_h - self._small.height) // 2,
              len(label), color=GREY).set(label)

    def _fits(self, text, glyphs, x=14):
        """Cells for `text`, bounded by what the panel can actually show.

        A Field wider than the panel does not fail: the driver clamps each blit
        to the edge, so the overflow lands in the wrong column and the line
        renders as a torn diagonal. Bounding the length here turns that into a
        truncation, which is at least legible and obviously wrong.
        """
        return min(len(text), (self.display.width - x) // glyphs.width)

    def _build_step_zones(self, top):
        """Draw the step grid and register each cell as it is drawn."""
        width = self.display.width
        for row_index, row in enumerate(STEP_ROWS):
            y = top + row_index * ZONE_ROW_H
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

    def set_hold_progress(self, fraction):
        """Show how far a touch-and-hold has got, 0.0 to 1.0.

        A bar along the top edge rather than on the target itself. Drawing over
        the target would mean repainting whatever it holds every frame, and the
        bar has to be cheap: this is called at the touch poll rate, which is
        three times the display rate.

        Redrawn only when the width actually changes by a visible amount. At
        30 Hz an unfiltered version repaints the same pixels thirty times a
        second and competes with the DRO for the SPI bus.
        """
        width = int(self.display.width * min(max(fraction, 0.0), 1.0))
        if abs(width - self._hold_width) < 4 and not (width == 0 < self._hold_width):
            return
        if width > self._hold_width:
            self.display.fill_rect(self._hold_width, 4,
                                   width - self._hold_width, HOLD_BAR_H, AMBER)
        else:
            self.display.fill_rect(width, 4,
                                   self.display.width - width, HOLD_BAR_H, BLACK)
        self._hold_width = width

    def set_position(self, values):
        self._latest["position"] = values
        if self.page != 0:
            return
        for axis, value in zip(AXES, values):
            if self._last_position.get(axis) == value:
                continue
            self._last_position[axis] = value
            self._positions[axis].set("{:.3f}".format(value))

    def set_state(self, state):
        self._latest["state"] = state
        if self.page != 0:
            return
        self._state.set(state, STATE_COLORS.get(state, WHITE))

    def set_mode(self, axis, step, feed=0.0):
        self._latest["mode"] = (axis, step, feed)
        if self.page != 0:
            return
        # Feed only. Distance per detent never changes, so a rising number
        # here is the only visible sign that winding faster is doing anything -
        # whereas axis and step are already shown by which row and which cell
        # is outlined.
        #
        # Eight cells holds "F15000" with room to spare, where the old combined
        # line needed 22 and had 20, and quietly truncated F15000 to F150.
        self._mode.set("F{:.0f}".format(feed))
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
        self._latest["link"] = connected
        if self.page != 0:
            return
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
