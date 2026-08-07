"""Validate the quadrature decode table on a PC, with no board attached.

Stubs `machine` so the real Quadrature class can be imported and driven with
fake pins. This exercises the shipped transition table rather than a copy of
it, so the test cannot drift away from the code it is checking.

    python tools/test_quadrature.py
"""

import sys
import types
from pathlib import Path


# --- stub out `machine` before importing the module under test -------------

class _FakePin:
    IN = 0
    OUT = 1
    PULL_UP = 1
    PULL_DOWN = 2
    IRQ_RISING = 1
    IRQ_FALLING = 2

    def __init__(self, ident, mode=None, pull=None):
        self.ident = ident
        self.pull = pull
        self._value = 0
        self.handler = None

    def value(self, val=None):
        if val is None:
            return self._value
        self._value = val

    def irq(self, handler=None, trigger=0, hard=False):
        self.handler = handler


_machine = types.ModuleType("machine")
_machine.Pin = _FakePin
sys.modules["machine"] = _machine

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "micropython"))
from pendant.quadrature import Quadrature, COUNTS_PER_DETENT  # noqa: E402


# --- helpers ---------------------------------------------------------------

# State is (A << 1) | B. One full quadrature cycle in each direction.
FORWARD = [(0, 0), (0, 1), (1, 1), (1, 0)]
REVERSE = list(reversed(FORWARD))

failures = []


def check(label, got, want):
    ok = got == want
    print("  {:<44} {}  (got {}, want {})".format(
        label, "PASS" if ok else "FAIL", got, want))
    if not ok:
        failures.append(label)


def drive(enc, sequence):
    """Apply a list of (a, b) states, firing the ISR after each."""
    for a, b in sequence:
        enc._pin_a.value(a)
        enc._pin_b.value(b)
        enc._isr(None)


def new_encoder(start=(0, 0)):
    enc = Quadrature(2, 3)
    enc._pin_a.value(start[0])
    enc._pin_b.value(start[1])
    enc.reset()
    return enc


# --- tests -----------------------------------------------------------------

print("quadrature decode")

enc = new_encoder()
drive(enc, FORWARD[1:] + [FORWARD[0]])
check("one forward revolution step -> +4 counts", enc.counts, 4)
check("  and exactly 1 detent", enc.detents, 1)
check("  with no illegal transitions", enc.errors, 0)

enc = new_encoder()
drive(enc, REVERSE + [FORWARD[0]][:0])
check("one reverse step -> -4 counts", enc.counts, -4)
check("  and exactly -1 detent", enc.detents, -1)
check("  with no illegal transitions", enc.errors, 0)

enc = new_encoder()
for _ in range(25):
    drive(enc, FORWARD[1:] + [FORWARD[0]])
check("25 detents forward -> 100 counts", enc.counts, 25 * COUNTS_PER_DETENT)
check("  reads as 25 detents", enc.detents, 25)

# Direction reversal must not accumulate drift.
enc = new_encoder()
for _ in range(10):
    drive(enc, FORWARD[1:] + [FORWARD[0]])
for _ in range(10):
    drive(enc, REVERSE + [(0, 0)][:0])
    drive(enc, [(0, 0)])
check("10 forward then 10 back -> net 0", enc.counts, 0)
check("  no errors across reversals", enc.errors, 0)

# A jump across two states at once is electrically impossible.
enc = new_encoder()
enc._pin_a.value(1)
enc._pin_b.value(1)
enc._isr(None)
check("illegal 00->11 jump counted as error", enc.errors, 1)
check("  and does not move the count", enc.counts, 0)

# A spurious interrupt with no pin change is normal, not an error.
enc = new_encoder()
enc._isr(None)
enc._isr(None)
check("repeated ISR with no change -> no error", enc.errors, 0)
check("  and no count movement", enc.counts, 0)

# take() hands over the delta and leaves the counter at zero.
enc = new_encoder()
for _ in range(3):
    drive(enc, FORWARD[1:] + [FORWARD[0]])
first = enc.take()
second = enc.take()
check("take() returns accumulated counts", first, 12)
check("take() zeroes the counter", second, 0)

# Detents truncate toward zero rather than flooring, so a partial detent
# backwards does not read as a full one.
enc = new_encoder()
drive(enc, [FORWARD[1]])
check("single edge forward is 0 detents", enc.detents, 0)
enc = new_encoder()
drive(enc, [REVERSE[1]])
check("single edge backward is 0 detents", enc.detents, 0)

print()
if failures:
    print("{} FAILED: {}".format(len(failures), ", ".join(failures)))
    sys.exit(1)
print("all quadrature decode tests passed")
