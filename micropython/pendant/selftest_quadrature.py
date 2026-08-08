"""Hardware self-test for the quadrature decoder - no encoder required.

Synthesises quadrature on two output pins and reads it back on the decoder's
input pins, which exercises the parts the PC-side table test cannot: real pin
configuration, hard IRQ registration and dispatch, and how much speed headroom
actually exists before edges start getting missed.

Wiring - two jumper wires on the breadboard:

    GP16 (drive A)  ->  GP2 (read A)
    GP17 (drive B)  ->  GP3 (read B)

Run:

    python tools/on_board.py micropython/pendant/selftest_quadrature.py

Without the jumpers it reports "no edges seen" and exits rather than hanging,
so it is still a safe way to check the module imports and the IRQs register.
"""

import time
from machine import Pin

import sys

sys.path.insert(0, "/")

try:
    from quadrature import Quadrature, COUNTS_PER_DETENT
except ImportError:
    from pendant.quadrature import Quadrature, COUNTS_PER_DETENT

READ_A, READ_B = 2, 3
DRIVE_A, DRIVE_B = 16, 17

# States as (A, B), in forward quadrature order starting from parked (0, 0).
FORWARD = ((0, 0), (0, 1), (1, 1), (1, 0))

# Drive sequences that both start and end at parked (0, 0), so every write is
# a genuine transition no matter what the pins were doing beforehand. Driving
# the parked state first instead - the obvious way to write this - produces no
# edge when the pins already rest there, costing exactly one count and looking
# deceptively like a missed edge.
FORWARD_SEQ = FORWARD[1:] + (FORWARD[0],)
REVERSE_SEQ = tuple(reversed(FORWARD[1:])) + (FORWARD[0],)

EDGES_PER_DETENT = 4

failures = []


def check(label, got, want):
    ok = got == want
    print("  {:<46} {}  (got {}, want {})".format(
        label, "PASS" if ok else "FAIL", got, want))
    if not ok:
        failures.append(label)


def park(drive_a, drive_b):
    """Return the drive pins to (0, 0) and let the inputs settle."""
    drive_a.value(0)
    drive_b.value(0)
    time.sleep_ms(2)


def drive_cycles(drive_a, drive_b, detents, step_us, reverse=False):
    """Drive `detents` complete cycles from parked back to parked.

    Returns elapsed microseconds so the caller can report the edge rate that
    was actually achieved, rather than the one sleep_us was asked for - at
    short intervals the interpreter's own loop overhead dominates.
    """
    sequence = REVERSE_SEQ if reverse else FORWARD_SEQ
    started = time.ticks_us()
    for _ in range(detents):
        for a, b in sequence:
            drive_a.value(a)
            drive_b.value(b)
            time.sleep_us(step_us)
    return time.ticks_diff(time.ticks_us(), started)


def main():
    print("\nquadrature hardware self-test")
    print("=" * 46)
    print("  drive GP{}/GP{}  ->  read GP{}/GP{}".format(
        DRIVE_A, DRIVE_B, READ_A, READ_B))

    drive_a = Pin(DRIVE_A, Pin.OUT, value=0)
    drive_b = Pin(DRIVE_B, Pin.OUT, value=0)
    time.sleep_ms(5)

    enc = Quadrature(READ_A, READ_B)
    print("  decoder attached, IRQs registered\n")

    # Sanity: is anything actually connected?
    park(drive_a, drive_b)
    enc.reset()
    drive_cycles(drive_a, drive_b, 2, 500)
    time.sleep_ms(10)
    if enc.counts == 0:
        print("  no edges seen - are the jumpers in place?")
        print("    GP{} -> GP{}   and   GP{} -> GP{}".format(
            DRIVE_A, READ_A, DRIVE_B, READ_B))
        enc.deinit()
        return 1

    # --- direction and scaling ---
    park(drive_a, drive_b)
    enc.reset()
    drive_cycles(drive_a, drive_b, 25, 500)
    time.sleep_ms(10)
    check("25 detents forward -> counts", enc.counts, 25 * COUNTS_PER_DETENT)
    check("  reads as 25 detents", enc.detents, 25)
    check("  no illegal transitions", enc.errors, 0)

    park(drive_a, drive_b)
    enc.reset()
    drive_cycles(drive_a, drive_b, 25, 500, reverse=True)
    time.sleep_ms(10)
    check("25 detents reverse -> counts", enc.counts, -25 * COUNTS_PER_DETENT)
    check("  reads as -25 detents", enc.detents, -25)
    check("  no illegal transitions", enc.errors, 0)

    park(drive_a, drive_b)
    enc.reset()
    drive_cycles(drive_a, drive_b, 40, 400)
    drive_cycles(drive_a, drive_b, 40, 400, reverse=True)
    time.sleep_ms(10)
    check("40 forward then 40 back -> net 0", enc.counts, 0)
    check("  no drift across reversal", enc.errors, 0)

    # --- speed headroom ---
    # A 60 mm wheel spun hard reaches maybe 5 rev/s. On a 100 PPR wheel that is
    # 500 detents/s, i.e. 2000 edges/s. Push well past that and find where it
    # actually breaks. Rates are measured, not assumed: below roughly 50 us the
    # interpreter's loop overhead sets the pace, not the requested delay.
    print("\n  speed headroom (100 detents per run, rates measured):")
    print("    {:>9}  {:>13}  {:>8}  {:>7}".format(
        "us/state", "real edges/s", "detents", "errors"))
    for step_us in (500, 200, 100, 50, 20, 10, 0):
        park(drive_a, drive_b)
        enc.reset()
        elapsed_us = drive_cycles(drive_a, drive_b, 100, step_us)
        time.sleep_ms(10)
        edges = 100 * EDGES_PER_DETENT
        rate = (edges * 1000000) // elapsed_us if elapsed_us else 0
        ok = "ok" if enc.detents == 100 and enc.errors == 0 else "MISSED"
        print("    {:>9}  {:>13}  {:>8}  {:>7}  {}".format(
            step_us, rate, enc.detents, enc.errors, ok))

    print("\n  a hand-turned 100 PPR wheel peaks near 2000 edges/s;")
    print("  everything above that row is margin.")

    enc.deinit()
    drive_a.value(0)
    drive_b.value(0)

    print()
    if failures:
        print("{} FAILED: {}".format(len(failures), ", ".join(failures)))
        return 1
    print("all hardware quadrature tests passed")
    return 0


raise SystemExit(main())
