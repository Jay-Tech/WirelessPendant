"""Hardware self-test for the quadrature decoder - no encoder required.

Synthesises quadrature on two output pins and reads it back on the decoder's
input pins, which exercises the parts the PC-side table test cannot: real pin
configuration, hard IRQ registration and dispatch, and how much speed headroom
actually exists before edges start getting missed.

Wiring - two jumper wires on the breadboard:

    GP16 (drive A)  ->  GP2 (read A)
    GP17 (drive B)  ->  GP3 (read B)

Run:

    python -m mpremote connect id:7BE7DD09548134C0 run micropython/pendant/selftest_quadrature.py

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

# One full quadrature cycle, as (A, B). Reversed for the other direction.
FORWARD = ((0, 0), (0, 1), (1, 1), (1, 0))

failures = []


def check(label, got, want):
    ok = got == want
    print("  {:<46} {}  (got {}, want {})".format(
        label, "PASS" if ok else "FAIL", got, want))
    if not ok:
        failures.append(label)


def emit(drive_a, drive_b, detents, step_us, reverse=False):
    """Drive `detents` full cycles, holding each state for step_us."""
    states = tuple(reversed(FORWARD)) if reverse else FORWARD
    for _ in range(detents):
        for a, b in states:
            drive_a.value(a)
            drive_b.value(b)
            time.sleep_us(step_us)


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
    emit(drive_a, drive_b, 2, 500)
    time.sleep_ms(10)
    if enc.counts == 0:
        print("  no edges seen - are the jumpers in place?")
        print("    GP{} -> GP{}   and   GP{} -> GP{}".format(
            DRIVE_A, READ_A, DRIVE_B, READ_B))
        enc.deinit()
        return 1

    # --- direction and scaling ---
    enc.reset()
    emit(drive_a, drive_b, 25, 500)
    time.sleep_ms(10)
    check("25 detents forward -> counts", enc.counts, 25 * COUNTS_PER_DETENT)
    check("  reads as 25 detents", enc.detents, 25)
    check("  no illegal transitions", enc.errors, 0)

    enc.reset()
    emit(drive_a, drive_b, 25, 500, reverse=True)
    time.sleep_ms(10)
    check("25 detents reverse -> counts", enc.counts, -25 * COUNTS_PER_DETENT)
    check("  reads as -25 detents", enc.detents, -25)

    enc.reset()
    emit(drive_a, drive_b, 40, 400)
    emit(drive_a, drive_b, 40, 400, reverse=True)
    time.sleep_ms(10)
    check("40 forward then 40 back -> net 0", enc.counts, 0)
    check("  no drift across reversal", enc.errors, 0)

    # --- speed headroom ---
    # A 60 mm wheel spun hard reaches maybe 5 rev/s. On a 100 PPR wheel that is
    # 500 detents/s, i.e. 2000 edges/s, i.e. 500 us per state. Push well past
    # that and find where it actually breaks.
    print("\n  speed headroom (100 detents per run):")
    print("    {:>9}  {:>11}  {:>8}  {:>7}".format(
        "us/state", "edges/s", "detents", "errors"))
    for step_us in (500, 200, 100, 50, 20, 10):
        enc.reset()
        emit(drive_a, drive_b, 100, step_us)
        time.sleep_ms(10)
        edges_per_s = 1000000 // step_us
        ok = "ok" if enc.detents == 100 and enc.errors == 0 else "MISSED"
        print("    {:>9}  {:>11}  {:>8}  {:>7}  {}".format(
            step_us, edges_per_s, enc.detents, enc.errors, ok))

    print("\n  a hand-turned wheel sits near the 500 us row;")
    print("  everything above that is margin.")

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
