"""FT6336U capacitive touch, reduced to taps.

The pendant does not need gestures, drag or multitouch. It needs to know that
somewhere was tapped, and where - so that is all this reports, and everything
else the controller offers is deliberately dropped.

Taps fire on press rather than release. Nothing a touch selects here moves the
machine - axis and step are selections, not commands - so the responsiveness is
free, and waiting for release makes a pendant feel unresponsive in a way that
matters when standing at a machine with a part in it.

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
        self._last_tap = time.ticks_add(time.ticks_ms(), -DEBOUNCE_MS)
        self.present = False
        self.chip_id = None
        self.stats = {"taps": 0, "errors": 0}

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
        """Return (x, y) for a new tap, or None.

        Returns at most one tap per press. A finger held down produces nothing
        after the first, and the debounce bounds how fast repeated taps can
        arrive however quickly the panel reports them.
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
            self._down = False
            return None

        if self._down:
            return None                      # still the same press
        self._down = True

        now = time.ticks_ms()
        if time.ticks_diff(now, self._last_tap) < DEBOUNCE_MS:
            return None
        self._last_tap = now

        x = ((data[1] & 0x0F) << 8) | data[2]
        y = ((data[3] & 0x0F) << 8) | data[4]
        self.stats["taps"] += 1
        return x, y


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
