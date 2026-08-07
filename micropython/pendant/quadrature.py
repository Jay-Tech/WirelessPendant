"""Quadrature decoder for an MPG handwheel.

Full 4x decode driven by pin interrupts on both edges of both channels. A
100 PPR handwheel produces 100 quadrature cycles per revolution and has 100
detents, so one detent is one full cycle: `counts` advances by 4 per detent
and `detents` divides that back down.

Why interrupts and not PIO: a hand-turned 60 mm wheel tops out around 5 rev/s,
which is 500 detents/s or 2000 edges/s - two orders of magnitude inside what
a hard IRQ handles. PIO would be the right answer for a motor-mounted encoder
spinning thousands of RPM, where edges arrive faster than any interrupt can
service. It is not needed for a wheel a human turns, and the state machine
below is far easier to verify.

The handler is registered with hard=True so a slow SPI display refresh cannot
delay it into missing edges. Hard IRQ handlers must not allocate, so all
mutable state lives in a preallocated array and the transition table is a
module-level tuple.
"""

from array import array
from machine import Pin

# Indexed by (previous_state << 2) | current_state, where state is (A << 1) | B.
# Yields -1, 0 or +1. The zeros on entries like 0b0011 are transitions that
# skip a Gray-code step - electrically impossible unless an edge was missed or
# a contact bounced, so they are counted as errors rather than guessed at.
_TRANSITION = (
    0, 1, -1, 0,
    -1, 0, 0, 1,
    1, 0, 0, -1,
    0, -1, 1, 0,
)

# Transitions where the state did not change at all (0b0000, 0b0101, ...) are
# legitimate no-ops from a spurious interrupt, not errors.
_UNCHANGED = (0, 5, 10, 15)

_COUNT = 0
_PREV = 1
_ERRORS = 2

COUNTS_PER_DETENT = 4


class Quadrature:
    """Reads an A/B quadrature encoder into a signed count."""

    def __init__(self, pin_a, pin_b, pull=None):
        """pull=None suits an encoder with external pull-ups, which is the
        right wiring for an open-collector handwheel. Pin.PULL_UP works for a
        bare mechanical encoder with no external parts. Do not use PULL_DOWN
        on RP2350 A2 silicon - erratum E9 can latch the input near 2.15 V.
        """
        self._pin_a = Pin(pin_a, Pin.IN, pull)
        self._pin_b = Pin(pin_b, Pin.IN, pull)

        self._state = array("i", [0, 0, 0])
        self._state[_PREV] = (self._pin_a.value() << 1) | self._pin_b.value()

        trigger = Pin.IRQ_RISING | Pin.IRQ_FALLING
        self._pin_a.irq(self._isr, trigger, hard=True)
        self._pin_b.irq(self._isr, trigger, hard=True)

    def _isr(self, _pin):
        state = self._state
        current = (self._pin_a.value() << 1) | self._pin_b.value()
        index = (state[_PREV] << 2) | current
        step = _TRANSITION[index]
        if step:
            state[_COUNT] += step
        elif index not in _UNCHANGED:
            state[_ERRORS] += 1
        state[_PREV] = current

    @property
    def counts(self):
        """Raw quadrature edges, signed. Four per detent."""
        return self._state[_COUNT]

    @property
    def detents(self):
        """Counts scaled to handwheel clicks, signed and truncated toward zero."""
        raw = self._state[_COUNT]
        return int(raw / COUNTS_PER_DETENT)

    @property
    def errors(self):
        """Illegal state transitions seen - missed edges or contact bounce.

        Should stay at zero. A climbing value means the wiring is noisy, the
        encoder is being spun implausibly fast, or pull-ups are missing.
        """
        return self._state[_ERRORS]

    def reset(self):
        self._state[_COUNT] = 0
        self._state[_ERRORS] = 0
        self._state[_PREV] = (self._pin_a.value() << 1) | self._pin_b.value()

    def take(self):
        """Return counts accumulated since the last call, and zero the total.

        The read-and-clear is not atomic against the IRQ; an edge landing
        between them is picked up on the following call rather than lost.
        """
        value = self._state[_COUNT]
        self._state[_COUNT] -= value
        return value

    def deinit(self):
        self._pin_a.irq(None)
        self._pin_b.irq(None)
