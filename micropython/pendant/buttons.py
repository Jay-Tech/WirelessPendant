"""Debounced buttons for the pendant.

Buttons wire between a GPIO and ground, using the internal pull-up so pressing
pulls the pin low. Pull-up rather than pull-down is deliberate: erratum
RP2350-E9 affects inputs resting on a weak pull-down, where the pin can latch
near 2.15 V instead of reading low. A pull-up input driven hard to ground by a
switch never enters that condition.

An unwired pin sits high and simply reads as "not pressed", so a partially
populated panel works without configuration - wire the buttons you have and
leave the rest of the map alone.

The debounce state machine is separated from the pin so it can be tested with
injected time, without hardware and without waiting in real time for bounce
intervals to elapse.
"""

from machine import Pin

# Mechanical switches bounce for a few milliseconds. 20 ms is comfortably past
# that for tactile switches while staying far below the ~150 ms where a button
# starts to feel unresponsive.
DEBOUNCE_MS = 20

# Hold time that turns a press into a long press. Used for actions that would
# be costly to trigger by accident, like zeroing an axis.
LONG_PRESS_MS = 800

PRESS = "press"
RELEASE = "release"
LONG_PRESS = "long"


class Debouncer:
    """Raw level plus time in, stable transitions out.

    Pure logic - no pin, no clock of its own - so the behaviour can be driven
    from a test at whatever timestamps it likes.
    """

    def __init__(self, debounce_ms=DEBOUNCE_MS):
        self.debounce_ms = debounce_ms
        self.stable = False
        self._candidate = False
        self._since = 0

    def update(self, pressed, now_ms):
        """Returns True on a settled press, False on a settled release, else None."""
        if pressed != self._candidate:
            # Level moved; restart the settling window rather than trusting it.
            self._candidate = pressed
            self._since = now_ms
            return None

        if pressed == self.stable:
            return None

        if now_ms - self._since >= self.debounce_ms:
            self.stable = pressed
            return pressed

        return None


class Button:
    """One physical button, reporting press / long press / release."""

    def __init__(self, pin, action, debounce_ms=DEBOUNCE_MS,
                 long_press_ms=LONG_PRESS_MS, active_low=True):
        self.action = action
        self.active_low = active_low
        self.long_press_ms = long_press_ms
        self._pin = Pin(pin, Pin.IN, Pin.PULL_UP if active_low else None)
        self._debouncer = Debouncer(debounce_ms)
        self._pressed_at = 0
        self._long_fired = False

    def _raw(self):
        value = self._pin.value()
        return value == 0 if self.active_low else value == 1

    def poll(self, now_ms, raw=None):
        """Advance one poll. Returns an event name, or None.

        `raw` overrides the pin read, which is what lets the panel be driven
        from a test without any hardware present.
        """
        if raw is None:
            raw = self._raw()

        edge = self._debouncer.update(raw, now_ms)

        if edge is True:
            self._pressed_at = now_ms
            self._long_fired = False
            return PRESS

        if edge is False:
            return RELEASE

        # Long press fires once, while still held, rather than on release -
        # so the operator gets feedback at the moment it takes effect instead
        # of discovering it afterwards.
        if (self._debouncer.stable and not self._long_fired
                and now_ms - self._pressed_at >= self.long_press_ms):
            self._long_fired = True
            return LONG_PRESS

        return None


class ButtonPanel:
    """A set of buttons polled together."""

    def __init__(self, mapping, **kwargs):
        """mapping: iterable of (pin, action) pairs."""
        self.buttons = [Button(pin, action, **kwargs) for pin, action in mapping]

    def poll(self, now_ms):
        """Returns a list of (action, event) for everything that changed."""
        events = []
        for button in self.buttons:
            event = button.poll(now_ms)
            if event is not None:
                events.append((button.action, event))
        return events
