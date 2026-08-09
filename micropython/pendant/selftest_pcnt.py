"""Hardware self-test for the PCNT quadrature decoder - no encoder required.

The ESP32 counterpart of selftest_quadrature.py, and deliberately the same
tests: synthesise quadrature on two output pins, read it back through PCNT,
and check the scale, the direction and the speed headroom. Same numbers on
both platforms means a straight comparison rather than a judgement call.

It matters more here than on the RP2350, because PCNT reports no errors of its
own. The scale check *is* the error check: 25 detents must read exactly 100
counts, and any missed or invented edge shows up as a count that is not a
multiple of four.

Wiring - two jumper wires:

    GP18 (drive A)  ->  GP16 (read A)
    GP19 (drive B)  ->  GP17 (read B)

Run, with the board's own port - never `connect auto`, which may find the Pico:

    python -m mpremote connect COM5 cp micropython/pendant/quadrature_pcnt.py :
    python -m mpremote connect COM5 run micropython/pendant/selftest_pcnt.py

Without the jumpers it reports "no edges seen" and exits rather than hanging,
so it is still a safe way to check that PCNT exists and configures.
"""

import time
from machine import Pin

import sys

sys.path.insert(0, "/")

try:
    from quadrature_pcnt import Quadrature, COUNTS_PER_DETENT
except ImportError:
    from pendant.quadrature_pcnt import Quadrature, COUNTS_PER_DETENT

# Safe on an ESP-WROOM-32: clear of the flash pins (6-11), the strapping pins
# (0, 2, 12, 15) and the input-only range (34-39), which cannot drive at all.
# On a WROVER, 16 and 17 belong to the PSRAM - move the read pair to 25/26.
READ_A, READ_B = 16, 17
DRIVE_A, DRIVE_B = 18, 19

# States as (A, B), in forward quadrature order starting from parked (0, 0).
FORWARD = ((0, 0), (0, 1), (1, 1), (1, 0))

# Drive sequences that both start and end at parked (0, 0), so every write is
# a genuine transition no matter what the pins were doing beforehand.
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
    was actually achieved rather than the one sleep_us was asked for.
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
    print("\nPCNT quadrature hardware self-test")
    print("=" * 46)
    print("  drive GP{}/GP{}  ->  read GP{}/GP{}".format(
        DRIVE_A, DRIVE_B, READ_A, READ_B))

    drive_a = Pin(DRIVE_A, Pin.OUT, value=0)
    drive_b = Pin(DRIVE_B, Pin.OUT, value=0)
    time.sleep_ms(5)

    enc = Quadrature(READ_A, READ_B)
    print("  PCNT unit configured, both channels, counter running\n")

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
    # A wrong sign here means the axis jogs the wrong way. It is the one thing
    # the PC-side table test cannot catch, because the channel modes only
    # exist in hardware.
    park(drive_a, drive_b)
    enc.reset()
    drive_cycles(drive_a, drive_b, 25, 500)
    time.sleep_ms(10)
    check("25 detents forward -> counts", enc.counts, 25 * COUNTS_PER_DETENT)
    check("  reads as 25 detents", enc.detents, 25)

    park(drive_a, drive_b)
    enc.reset()
    drive_cycles(drive_a, drive_b, 25, 500, reverse=True)
    time.sleep_ms(10)
    check("25 detents reverse -> counts", enc.counts, -25 * COUNTS_PER_DETENT)
    check("  reads as -25 detents", enc.detents, -25)

    park(drive_a, drive_b)
    enc.reset()
    drive_cycles(drive_a, drive_b, 40, 400)
    drive_cycles(drive_a, drive_b, 40, 400, reverse=True)
    time.sleep_ms(10)
    check("40 forward then 40 back -> net 0", enc.counts, 0)

    # --- take() is lossless ---
    # value(0) clears the counter in a separate step from reading it and drops
    # whatever lands in between, so take() subtracts instead. Proving that here
    # is cheap; discovering it as a slowly drifting axis is not.
    park(drive_a, drive_b)
    enc.reset()
    drive_cycles(drive_a, drive_b, 10, 400)
    time.sleep_ms(10)
    first = enc.take()
    drive_cycles(drive_a, drive_b, 10, 400)
    time.sleep_ms(10)
    second = enc.take()
    check("take() returns the first batch", first, 10 * COUNTS_PER_DETENT)
    check("  and only the second batch after", second,
          10 * COUNTS_PER_DETENT)
    check("  leaving nothing behind", enc.take(), 0)

    # --- wrap handling ---
    # Past LIMIT the counter resets to zero and the total is only right if the
    # overflow interrupt was folded in. Reaching it takes 25 revolutions, so
    # nothing else in this file would ever exercise it.
    from quadrature_pcnt import LIMIT
    detents_past = (LIMIT // COUNTS_PER_DETENT) + 50
    park(drive_a, drive_b)
    enc.reset()
    print("\n  driving {} detents to cross the {} count wrap...".format(
        detents_past, LIMIT))
    drive_cycles(drive_a, drive_b, detents_past, 0)
    time.sleep_ms(10)
    check("count survives the counter wrap", enc.counts,
          detents_past * COUNTS_PER_DETENT)

    # --- speed headroom ---
    # A 60 mm wheel spun hard reaches maybe 5 rev/s. On a 100 PPR wheel that is
    # 500 detents/s, i.e. 2000 edges/s. The hardware should not miss anything
    # at any rate the drive loop can produce - that is the whole point of it -
    # so a MISSED row here means the configuration is wrong, not that a limit
    # was found.
    print("\n  speed headroom (100 detents per run, rates measured):")
    print("    {:>9}  {:>13}  {:>8}".format(
        "us/state", "real edges/s", "detents"))
    for step_us in (500, 200, 100, 50, 20, 10, 0):
        park(drive_a, drive_b)
        enc.reset()
        elapsed_us = drive_cycles(drive_a, drive_b, 100, step_us)
        time.sleep_ms(10)
        edges = 100 * EDGES_PER_DETENT
        rate = (edges * 1000000) // elapsed_us if elapsed_us else 0
        ok = "ok" if enc.detents == 100 else "MISSED"
        print("    {:>9}  {:>13}  {:>8}  {}".format(
            step_us, rate, enc.detents, ok))

    print("\n  a hand-turned 100 PPR wheel peaks near 2000 edges/s;")
    print("  the hardware filter is set at 12.8 us, so the fastest rows here")
    print("  are the ones that would show a filter set too aggressively.")

    enc.deinit()
    drive_a.value(0)
    drive_b.value(0)

    print()
    if failures:
        print("{} FAILED: {}".format(len(failures), ", ".join(failures)))
        return 1
    print("all PCNT quadrature tests passed")
    return 0


raise SystemExit(main())
