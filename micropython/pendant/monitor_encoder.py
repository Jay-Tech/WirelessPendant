"""Live readout from the handwheel. First check against real hardware.

Prints position, direction and rate as you turn, so you can confirm the wiring
end to end: that both channels are connected, that clockwise counts up, and
that one physical click produces exactly one detent.

Wire the divider junctions to GP2 (A) and GP3 (B), then:

    python tools/on_board.py micropython/pendant/monitor_encoder.py

Ctrl-C to stop. What to look for:

* One click of the wheel = one detent. If a click gives 2 or 4, the wheel is
  not 1 detent per quadrature cycle and COUNTS_PER_DETENT needs changing.
* errors stays at 0. A climbing count means electrical noise or a bad joint.
* Only one channel moving means the other is not connected - a single channel
  still produces edges, so a half-wired encoder looks alive but counts wrong.
"""

import sys
import time
from machine import Pin

# Same board split as pendant.py, and for the same reason: the RP2350 decoder
# uses a hard pin IRQ the ESP32 port does not have, and the ESP32 counts in
# PCNT hardware instead. A monitor pinned to one of them measures the wrong
# board silently - it simply reads nothing and looks like a wiring fault.
_ESP32 = sys.platform == "esp32"

if _ESP32:
    try:
        from quadrature_pcnt import Quadrature, COUNTS_PER_DETENT
    except ImportError:
        from pendant.quadrature_pcnt import Quadrature, COUNTS_PER_DETENT
    PIN_A, PIN_B = 9, 10
else:
    try:
        from quadrature import Quadrature, COUNTS_PER_DETENT
    except ImportError:
        from pendant.quadrature import Quadrature, COUNTS_PER_DETENT
    PIN_A, PIN_B = 2, 3

REFRESH_MS = 100

# Bounded rather than endless. Driven through `mpremote`, output does not reach
# the operator until the run finishes, so "Ctrl-C when you have seen enough"
# means seeing nothing until you guess it is time to stop. A fixed window turns
# it into: start it, turn the wheel until it ends, read the summary.
#
# Ctrl-C still works and still summarises, for anyone running it over a
# terminal where the live rows are visible.
MONITOR_SECONDS = 30


def main():
    print("\nhandwheel monitor")
    print("=" * 52)
    print("  reading A on GP{}, B on GP{}".format(PIN_A, PIN_B))

    encoder = Quadrature(PIN_A, PIN_B)
    pin_a = Pin(PIN_A, Pin.IN)
    pin_b = Pin(PIN_B, Pin.IN)

    print("  idle levels: A={} B={}  (both should be 0 or 1, not floating)".format(
        pin_a.value(), pin_b.value()))
    print("\n  turn the wheel for {} s - Ctrl-C stops early".format(
        MONITOR_SECONDS))
    print("  one click is 4 counts; a click landing across a sample boundary")
    print("  shows as two rows summing to 4 (1+3, 2+2), which is not an error\n")
    print("  {:>9} {:>9} {:>7} {:>8} {:>10} {:>6}".format(
        "detents", "counts", "d.count", "errors", "detents/s", "dir"))

    last_counts = 0
    last_time = time.ticks_ms()
    peak_rate = 0.0

    deadline = time.ticks_add(time.ticks_ms(), MONITOR_SECONDS * 1000)

    try:
        while time.ticks_diff(deadline, time.ticks_ms()) > 0:
            time.sleep_ms(REFRESH_MS)

            counts = encoder.counts
            now = time.ticks_ms()
            elapsed_ms = time.ticks_diff(now, last_time)
            delta = counts - last_counts
            last_counts, last_time = counts, now

            rate = 0.0
            if elapsed_ms > 0:
                rate = abs(delta) * 1000.0 / (COUNTS_PER_DETENT * elapsed_ms)
            if rate > peak_rate:
                peak_rate = rate

            if delta > 0:
                direction = "CW"
            elif delta < 0:
                direction = "CCW"
            else:
                direction = "-"

            print("  {:>9} {:>9} {:>+7} {:>8} {:>10.1f} {:>6}".format(
                encoder.detents, counts, delta, encoder.errors, rate,
                direction))

    except KeyboardInterrupt:
        pass
    finally:
        # Every figure read before deinit(), not after. `detents` and `counts`
        # both go through the PCNT hardware, so reading either from a decoder
        # that has been shut down gives a number that is wrong without looking
        # wrong - it reported -2500 against a table that ended at -4173.
        counts = encoder.counts
        detents = encoder.detents
        errors = encoder.errors
        encoder.deinit()

        print("\n  final: {} detents, {} counts, {} errors".format(
            detents, counts, errors))
        print("  peak rate: {:.1f} detents/s".format(peak_rate))
        print("  idle levels now: A={} B={}".format(
            pin_a.value(), pin_b.value()))

        if errors:
            print("  errors seen - check joints and that both channels are wired")
        elif _ESP32:
            # PCNT raises nothing of its own, so a zero error count on the
            # ESP32 means only that nothing was counting errors. The scale is
            # the check instead: leave the wheel resting in a detent and the
            # total must be a whole number of quadrature cycles. A remainder is
            # an edge that was missed or invented, which is exactly the symptom
            # an under-driven encoder would produce.
            # abs() first. Turning the wheel anticlockwise makes counts
            # negative, and Python's modulo of a negative number is the
            # non-negative complement - so 16695 counts CCW reported "1 left
            # over" where the wheel was really 3 counts into the next detent.
            # The sign is direction; it has nothing to do with the scale.
            magnitude = abs(counts)
            remainder = magnitude % COUNTS_PER_DETENT
            print("  {} counts is {} whole detents and {} counts over".format(
                counts, magnitude // COUNTS_PER_DETENT, remainder))
            if not remainder:
                print("  clean scale - every count formed a whole detent")
            else:
                # A remainder of one to three counts is where the wheel stopped,
                # not evidence of anything. It only means lost edges if the
                # wheel is genuinely detented and sitting in one, and a hand
                # leaving it mid-click is the ordinary case - so this says what
                # it saw rather than pronouncing on it.
                print("  {} of {} counts into the next detent. That is where".format(
                    remainder, COUNTS_PER_DETENT))
                print("  the wheel stopped unless it is resting hard in a")
                print("  detent, in which case it is a lost or invented edge.")
                print("  {} counts total, so this is {:.3f}% either way.".format(
                    magnitude, 100.0 * remainder / magnitude))
        else:
            print("  clean run, no illegal transitions")

        if counts == 0:
            print("\n  Nothing counted at all. Either the wheel was not turned,")
            print("  or the levels above never moved - check the supply first,")
            print("  which is the thing under test if this is the 3V3 trial.")


main()
