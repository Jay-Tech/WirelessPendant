"""Quadrature decoder for an MPG handwheel, counting in ESP32 PCNT hardware.

Presents the same surface as `quadrature.Quadrature`, so the scheduler cannot
tell which one it was handed.

It exists because the RP2350 version cannot simply be ported. That one takes
its pin IRQ with hard=True, and the ESP32 port has no hard IRQ support at all
- `Pin.irq(hard=True)` raises TypeError there. A soft IRQ would put edge
counting behind the interpreter, where a 320x480 SPI blit holds it off long
enough for MicroPython's scheduler queue to overflow and drop callbacks
silently. A handwheel that undercounts while the screen redraws is the worst
bug this device could have, so software decoding is not an option on ESP32.

PCNT counts in silicon, which the display cannot delay. That is a better
answer than the one it replaces, with one loss: the software decoder rejects
illegal Gray-code transitions and counts them, and hardware cannot tell you it
was fed nonsense. `errors` therefore stays at zero rather than lying about
having checked - see its docstring for what replaces it.

Needs `esp32.PCNT`, which is compiled in wherever the silicon has the
peripheral: MICROPY_PY_ESP32_PCNT defaults to SOC_PCNT_SUPPORTED, true for the
original ESP32, S2, S3 and C6. The C3 has no pulse counter and cannot run
this.
"""

import esp32
from machine import Pin

if not hasattr(esp32, "PCNT"):
    raise ImportError(
        "esp32.PCNT missing: this part has no pulse counter (the C3 has "
        "none). There is no software fallback worth having, because the "
        "ESP32 port has no hard pin IRQ to build one on.")

COUNTS_PER_DETENT = 4

# The counter resets to zero on reaching these and raises IRQ_MAX / IRQ_MIN;
# the handler folds the wrap into an accumulator. Anything up to 32767 is
# legal. 10000 is 25 revolutions of a 100 PPR wheel, which is often enough
# that the accumulator is exercised in ordinary jogging rather than only after
# an hour, and rare enough that the interrupt costs nothing.
LIMIT = 10000

# Hardware glitch filter, in APB clock cycles - 80 MHz, so the 1023 maximum is
# 12.8 us. The shortest gap a hand can produce is around 500 us, at 5 rev/s on
# a 100 PPR wheel, so even the longest filter available sits two orders of
# magnitude clear of a real edge. This is what removes the contact bounce the
# software decoder could only detect after the fact.
FILTER_CYCLES = 1023


class Quadrature:
    """Reads an A/B quadrature encoder into a signed count, in hardware."""

    def __init__(self, pin_a, pin_b, pull=None, unit=0,
                 filter_cycles=FILTER_CYCLES, invert=False):
        """pull=None suits an encoder with external pull-ups, which is the
        right wiring for an open-collector handwheel, and matches the divider
        described in the README. Pin.PULL_UP works for a bare mechanical
        encoder with no external parts.

        `unit` selects a PCNT unit: 0-7 on the original ESP32, 0-3 on the S3.
        `invert` swaps the counting sense, for when A and B arrive the other
        way round in a connector and resoldering is the worse option.
        """
        self._pin_a = Pin(pin_a, Pin.IN, pull)
        self._pin_b = Pin(pin_b, Pin.IN, pull)

        # Sign matches the software decoder's transition table, where 00 -> 01
        # is +1: B leading A reads as forward. Getting this backwards jogs the
        # axis the wrong way, which is obvious on a bench but worth stating
        # rather than discovering.
        up, down = esp32.PCNT.INCREMENT, esp32.PCNT.DECREMENT
        if invert:
            up, down = down, up

        # Full 4x decode takes both channels of one unit: each counts the
        # edges of one signal and takes its direction from the other.
        # mode_high=REVERSE flips the sense while the other signal is high,
        # which is what turns two 2x channels into a single 4x count.
        #
        # Walking the forward sequence 00 -> 01 -> 11 -> 10 -> 00 gives +1 at
        # every one of the four edges, and the reverse sequence -1 at each.
        self._pcnt = esp32.PCNT(
            unit, channel=0,
            pin=self._pin_a, mode_pin=self._pin_b,
            rising=down, falling=up,
            mode_low=esp32.PCNT.NORMAL, mode_high=esp32.PCNT.REVERSE,
            # Unit-wide, so set once here rather than again on channel 1.
            filter=filter_cycles, min=-LIMIT, max=LIMIT, value=0)

        # Same unit id returns the same object, so this configures the second
        # channel rather than creating anything new.
        self._pcnt.init(
            channel=1,
            pin=self._pin_b, mode_pin=self._pin_a,
            rising=up, falling=down,
            mode_low=esp32.PCNT.NORMAL, mode_high=esp32.PCNT.REVERSE)

        # The IDF's PCNT driver configures its own input pins, applying its
        # own pull as it goes. Re-assert what the caller asked for: an
        # unwanted internal pull-up sits in parallel with the external divider
        # and lifts the high level above the rail.
        self._pin_a = Pin(pin_a, Pin.IN, pull)
        self._pin_b = Pin(pin_b, Pin.IN, pull)

        self._acc = 0      # counts folded in from wraps, by _on_limit
        self._zero = 0     # raw total already consumed by take() or reset()

        self._irq = self._pcnt.irq(
            self._on_limit, esp32.PCNT.IRQ_MIN | esp32.PCNT.IRQ_MAX)

        # Nothing counts until this. A freshly constructed unit is paused -
        # the constructor deinits it into a known state and never resumes.
        self._pcnt.start()

    def _on_limit(self, _pcnt):
        """Folds a counter wrap into the accumulator.

        Runs either from the scheduler or synchronously from inside value(),
        which flushes pending events so that the count it returns and the
        accumulator agree. Reading the flags is not bookkeeping that can be
        skipped: that flush loop spins until they are clear, so a handler
        which does not clear them hangs the interpreter.
        """
        flags = self._irq.flags()
        if flags & esp32.PCNT.IRQ_MAX:
            self._acc += LIMIT
        if flags & esp32.PCNT.IRQ_MIN:
            self._acc -= LIMIT

    def _raw(self):
        """Total signed edges since construction.

        The counter has to be read first. value() may run the overflow handler
        before returning, so evaluating self._acc first would pair an
        accumulator from before the wrap with a counter from after it - a
        10000 count jump, once every 25 revolutions.
        """
        counter = self._pcnt.value()
        return self._acc + counter

    @property
    def counts(self):
        """Raw quadrature edges, signed. Four per detent."""
        return self._raw() - self._zero

    @property
    def detents(self):
        """Counts scaled to handwheel clicks, signed and truncated toward zero."""
        return int(self.counts / COUNTS_PER_DETENT)

    @property
    def errors(self):
        """Always zero here: the hardware counts, it does not validate.

        The software decoder can spot a transition that skips a Gray-code step
        and call it an error. PCNT sees pulses, not states, so there is
        nothing equivalent to report and a fabricated number would be worse
        than none. Kept so callers need not know which decoder they hold.

        What replaces it is a scale check rather than an edge check: one full
        turn of a 100 PPR wheel must read exactly 400 counts. selftest_pcnt.py
        does this against synthesised edges; monitor_encoder.py does it
        against the real wheel.
        """
        return 0

    def reset(self):
        # Re-baselines instead of clearing the hardware. value(0) reads and
        # clears in two separate steps and loses anything landing between
        # them, and nothing here needs the counter to actually be zero.
        self._zero = self._raw()

    def take(self):
        """Return counts accumulated since the last call, and zero the total.

        Subtracts rather than clearing, for the reason reset() does: an edge
        arriving mid-call is carried into the following call rather than lost.
        """
        total = self._raw()
        delta = total - self._zero
        self._zero = total
        return delta

    def deinit(self):
        self._pcnt.irq(None)
        self._pcnt.deinit()
