"""FT6336U capacitive touch, reduced to taps and holds.

The pendant does not need gestures, drag or multitouch. It needs to know that
somewhere was tapped or held, and where - so that is all this reports, and
everything else the controller offers is deliberately dropped.

Taps fire on press rather than release. Nothing a touch selects here moves the
machine - axis and step are selections, not commands - so the responsiveness is
free, and waiting for release makes a pendant feel unresponsive in a way that
matters when standing at a machine with a part in it.

Holds are the opposite case and exist for the opposite reason. Anything that
drives the tool at the work has to be deliberate, and 800 ms of contact is hard
to do by accident where a tap is not. It matches the physical zero button,
which has wanted a hold since it was wired: one convention for "this one is
consequential", rather than a different gesture depending on which surface the
control happens to live on.

Coordinates come out in display space with no transform. That is not luck: the
panel is run in the same orientation the controller reports in, measured during
bring-up as x 0..319 and y 7..478 against a 320x480 display. Rotating the
display without rotating this would silently mirror every target.
"""

import time

from machine import I2C, Pin

FT6336U_ADDR = 0x38

_REG_TD_STATUS = 0x02
_REG_CHIPID = 0xA3
_REG_VENDORID = 0xA8

_FOCALTECH_VENDOR = 0x11

# Ignore a second tap this soon after the last. Capacitive panels report a
# touch for as long as a finger rests, and a pendant is held in a hand, so
# without this a resting thumb walks the step ladder continuously.
DEBOUNCE_MS = 250

# A press must be released before another is recognised, regardless of the
# debounce. Together those mean holding a finger down does nothing after the
# first event, which is what makes a zone safe to put next to a screen edge
# that gets gripped.
POLL_MS = 30

# Contact before a hold fires. Matches buttons.LONG_PRESS_MS deliberately - the
# operator should not have to learn that a screen hold and a button hold take
# different amounts of time.
HOLD_MS = 800

# Movement that cancels a hold, in pixels. A finger resting for most of a
# second wanders, and demanding it stay still would make the gesture feel
# broken; sliding off the target deliberately is how it gets abandoned.
HOLD_SLOP_PX = 40


class Touch:
    """Tap source for the FT6336U. Poll it; it returns (x, y) or None."""

    def __init__(self, sda, scl, rst=None, int_pin=None, i2c_id=1,
                 freq=400000):
        self._rst = Pin(rst, Pin.OUT, value=1) if rst is not None else None
        self.reset()

        # The interrupt line is read rather than used as a trigger. Polling at
        # 30 ms is well inside a finger's dwell time, and an IRQ here would put
        # I2C traffic in a hard interrupt context alongside the encoder's -
        # which is the one place on this board that cannot afford to be
        # delayed.
        self._int = (Pin(int_pin, Pin.IN, Pin.PULL_UP)
                     if int_pin is not None else None)

        self._i2c = I2C(i2c_id, sda=Pin(sda), scl=Pin(scl), freq=freq)
        self._down = False
        self._down_at = 0
        self._down_point = None
        self._held = False
        self._last_tap = time.ticks_add(time.ticks_ms(), -DEBOUNCE_MS)
        self.present = False
        self.chip_id = None
        self.stats = {"taps": 0, "holds": 0, "errors": 0}

        self._identify()

    def reset(self):
        """Pulse reset and wait for the controller to boot.

        The FT6x36 will not answer while reset is asserted, and a module that
        has been unpowered can come up that way - which presents as an empty
        bus and reads like a wiring fault.
        """
        if self._rst is None:
            return
        self._rst(0)
        time.sleep_ms(10)
        self._rst(1)
        time.sleep_ms(300)

    def _identify(self):
        try:
            if FT6336U_ADDR not in self._i2c.scan():
                return
            self.chip_id = self._i2c.readfrom_mem(
                FT6336U_ADDR, _REG_CHIPID, 1)[0]
            vendor = self._i2c.readfrom_mem(
                FT6336U_ADDR, _REG_VENDORID, 1)[0]
            self.present = vendor == _FOCALTECH_VENDOR
        except OSError:
            self.present = False

    def poll(self):
        """Return an event, or None.

        ("tap", x, y)      a press, once per contact
        ("hold", x, y)     the same contact still down after HOLD_MS
        ("progress", f)    fraction of the way to a hold, 0.0 to 1.0

        Progress exists so the screen can show a hold filling. A gesture that
        takes most of a second with no feedback is indistinguishable from one
        that is not being registered, and the operator lets go and tries again -
        which is the one thing a deliberate gesture must not encourage.
        """
        if not self.present:
            return None

        try:
            # One block read: the controller updates the point registers
            # together, so reading them separately can straddle an update and
            # pair an x from one touch with a y from the next.
            data = self._i2c.readfrom_mem(FT6336U_ADDR, _REG_TD_STATUS, 5)
        except OSError:
            self.stats["errors"] += 1
            return None

        touching = (data[0] & 0x0F) > 0
        if not touching:
            released = self._down
            self._down = False
            self._held = False
            self._down_point = None
            # A release has to be reported, not just noticed. Nothing else
            # tells the screen the gesture is over, so a bar left part-filled
            # sat there until the next touch happened to redraw it - which
            # reads as the pendant still waiting on a hold that was abandoned
            # a minute ago.
            if released:
                return "progress", 0.0
            return None

        x = ((data[1] & 0x0F) << 8) | data[2]
        y = ((data[3] & 0x0F) << 8) | data[4]
        now = time.ticks_ms()

        if not self._down:
            self._down = True
            self._held = False
            self._down_at = now
            self._down_point = (x, y)

            if time.ticks_diff(now, self._last_tap) < DEBOUNCE_MS:
                return None
            self._last_tap = now
            self.stats["taps"] += 1
            return "tap", x, y

        if self._held or self._down_point is None:
            return None

        # A finger resting for the best part of a second wanders. Only a move
        # far enough to be leaving the target abandons the hold.
        start_x, start_y = self._down_point
        if abs(x - start_x) > HOLD_SLOP_PX or abs(y - start_y) > HOLD_SLOP_PX:
            self._held = True                # abandoned, not fired
            return "progress", 0.0

        elapsed = time.ticks_diff(now, self._down_at)
        if elapsed >= HOLD_MS:
            self._held = True
            self.stats["holds"] += 1
            return "hold", start_x, start_y

        return "progress", elapsed / HOLD_MS
