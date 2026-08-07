"""Live readout from the handwheel. First check against real hardware.

Prints position, direction and rate as you turn, so you can confirm the wiring
end to end: that both channels are connected, that clockwise counts up, and
that one physical click produces exactly one detent.

Wire the divider junctions to GP2 (A) and GP3 (B), then:

    python -m mpremote connect id:7BE7DD09548134C0 run micropython/pendant/monitor_encoder.py

Ctrl-C to stop. What to look for:

* One click of the wheel = one detent. If a click gives 2 or 4, the wheel is
  not 1 detent per quadrature cycle and COUNTS_PER_DETENT needs changing.
* errors stays at 0. A climbing count means electrical noise or a bad joint.
* Only one channel moving means the other is not connected - a single channel
  still produces edges, so a half-wired encoder looks alive but counts wrong.
"""

import time
from machine import Pin

try:
    from quadrature import Quadrature, COUNTS_PER_DETENT
except ImportError:
    from pendant.quadrature import Quadrature, COUNTS_PER_DETENT

PIN_A, PIN_B = 2, 3
REFRESH_MS = 100


def main():
    print("\nhandwheel monitor")
    print("=" * 52)
    print("  reading A on GP{}, B on GP{}".format(PIN_A, PIN_B))

    encoder = Quadrature(PIN_A, PIN_B)
    pin_a = Pin(PIN_A, Pin.IN)
    pin_b = Pin(PIN_B, Pin.IN)

    print("  idle levels: A={} B={}  (both should be 0 or 1, not floating)".format(
        pin_a.value(), pin_b.value()))
    print("\n  turn the wheel - Ctrl-C to stop")
    print("  one click is 4 counts; a click landing across a sample boundary")
    print("  shows as two rows summing to 4 (1+3, 2+2), which is not an error\n")
    print("  {:>9} {:>9} {:>7} {:>8} {:>10} {:>6}".format(
        "detents", "counts", "d.count", "errors", "detents/s", "dir"))

    last_counts = 0
    last_time = time.ticks_ms()
    peak_rate = 0.0

    try:
        while True:
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
        encoder.deinit()
        print("\n  final: {} detents, {} counts, {} errors".format(
            encoder.detents, encoder.counts, encoder.errors))
        print("  peak rate: {:.1f} detents/s".format(peak_rate))
        if encoder.errors:
            print("  errors seen - check joints and that both channels are wired")
        else:
            print("  clean run, no illegal transitions")


main()
