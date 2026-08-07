"""Tests for the jog scheduler. Runs on a PC with a fake encoder.

    python tools/test_jog.py
"""

import sys
import types
from pathlib import Path

sys.modules.setdefault("machine", types.ModuleType("machine"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "micropython"))

from pendant import protocol  # noqa: E402
from pendant.jog import (JogScheduler, STEP_SIZES,  # noqa: E402
                         IDLE_TICKS_BEFORE_CANCEL, FEED_MIN_MM_MIN,
                         FEED_MAX_MM_MIN, RATE_WINDOW_TICKS, TICK_MS)

failures = []


def check(label, got, want):
    ok = got == want
    print("  {:<52} {}".format(label, "PASS" if ok else "FAIL"))
    if not ok:
        print("      got  {!r}\n      want {!r}".format(got, want))
        failures.append(label)


def motion(message):
    """The distance-carrying fields of a jog message, ignoring feed."""
    if message is None or message.get("t") != "jog":
        return message
    return {k: message[k] for k in ("t", "axis", "det", "step")}


class FakeEncoder:
    """Stands in for Quadrature: counts are pushed in by the test."""

    def __init__(self):
        self.pending = 0

    def move(self, counts):
        self.pending += counts

    def take(self):
        value = self.pending
        self.pending = 0
        return value


def new_scheduler(axis="X"):
    encoder = FakeEncoder()
    return encoder, JogScheduler(encoder, axis=axis)


print("coalescing")

enc, sched = new_scheduler()
enc.move(4)
check("one detent produces one jog message", motion(sched.tick()),
      {"t": "jog", "axis": "X", "det": 1, "step": 0.1})

enc, sched = new_scheduler()
enc.move(4 * 7)
check("seven detents in one tick coalesce into one message",
      motion(sched.tick()), {"t": "jog", "axis": "X", "det": 7, "step": 0.1})

enc, sched = new_scheduler()
check("no movement produces no message", sched.tick(), None)

enc, sched = new_scheduler()
enc.move(-4 * 3)
check("reverse rotation gives negative detents",
      motion(sched.tick()), {"t": "jog", "axis": "X", "det": -3, "step": 0.1})

print("\npartial detents")

# Slow turning delivers fewer than 4 counts per tick. Those must accumulate,
# not be rounded away every tick - otherwise slow turning moves nothing at all.
enc, sched = new_scheduler()
results = []
for _ in range(4):
    enc.move(1)
    results.append(motion(sched.tick()))
check("single counts accumulate rather than rounding away",
      results, [None, None, None,
                {"t": "jog", "axis": "X", "det": 1, "step": 0.1}])

enc, sched = new_scheduler()
enc.move(6)          # one detent plus a remainder of 2
first = sched.tick()
enc.move(2)          # remainder completes a second detent
second = sched.tick()
check("remainder carries into the following tick",
      (first["det"], second["det"]), (1, 1))

enc, sched = new_scheduler()
total = 0
for _ in range(20):
    enc.move(2)
    message = sched.tick()
    if message and message["t"] == "jog":
        total += message["det"]
check("slow steady turn loses no motion over 20 ticks", total, 10)

print("\nfeed tracking")

# The property the whole design rests on: distance is exactly one step per
# detent no matter how fast the wheel turns, and only the feed varies. Scaling
# distance instead makes how far the axis moved depend on how deep the queue
# was when you stopped, which is the run-off this exists to avoid.
per_detent = []
for rate in (1, 3, 6, 12):
    enc, sched = new_scheduler()
    for _ in range(RATE_WINDOW_TICKS * 2):
        enc.move(4 * rate)
        message = sched.tick()
    per_detent.append(abs(message["det"] * message["step"]) / rate)
check("distance per detent is identical at every turn speed",
      all(abs(d - 0.1) < 1e-9 for d in per_detent), True)

# A slow turn sits on the floor rather than crawling.
enc, sched = new_scheduler()
for _ in range(20):
    enc.move(4)
    slow = sched.tick()
    for _ in range(9):
        sched.tick()
check("slow turning feeds at the floor", slow["feed"], FEED_MIN_MM_MIN)

# Feed should equal the rate actually being commanded: detents/s x mm x 60.
enc, sched = new_scheduler()
sched.set_step_index(3)                  # 1.0 mm per detent
for _ in range(RATE_WINDOW_TICKS * 2):
    enc.move(4)                          # 1 detent per 20 ms = 50 detents/s
    tracked = sched.tick()
check("feed matches the commanded rate", tracked["feed"], 50 * 1.0 * 60)

# Capped, because asking for more than the machine can deliver only rebuilds
# the queue this design exists to keep shallow.
enc, sched = new_scheduler()
sched.set_step_index(3)
for _ in range(RATE_WINDOW_TICKS * 2):
    enc.move(4 * 6)                      # 300 detents/s at 1 mm
    capped = sched.tick()
check("feed is capped at the ceiling", capped["feed"], FEED_MAX_MM_MIN)
check("  while distance stays exact", capped["det"] * capped["step"], 6.0)

enc, sched = new_scheduler()
sched.feed_tracking = False
enc.move(4)
check("feed tracking can be disabled", sched.tick()["feed"], FEED_MAX_MM_MIN)

# A pause must not leave a high feed armed for the next careful detent - that
# would make the first move after a pause far faster than intended.
enc, sched = new_scheduler()
for _ in range(RATE_WINDOW_TICKS * 2):
    enc.move(4 * 6)                      # 300 detents/s at 0.1 mm
    spinning = sched.tick()
check("fast turning raises the feed", spinning["feed"], 300 * 0.1 * 60)

for _ in range(RATE_WINDOW_TICKS):
    sched.tick()                         # idle; the window fills with zeros
enc.move(4)
check("  and it decays back to the floor while idle",
      sched.tick()["feed"], FEED_MIN_MM_MIN)

print("\nrest dither")

# Observed on real hardware: a wheel coming to rest on a quadrature edge
# toggles one channel back and forth from vibration alone. These are legal
# transitions - the decoder's error count stays at zero - so the scheduler is
# what has to reject them, or a stationary wheel would trickle out jogs.
enc, sched = new_scheduler()
messages = []
for i in range(20):
    enc.move(1 if i % 2 == 0 else -1)
    messages.append(sched.tick())
check("+/-1 dither at rest emits nothing", messages, [None] * 20)

# The cancel threshold has to clear a slow turn. Someone winding continuously
# still produces a detent every few hundred ms; if the threshold fired inside
# that gap it would flush motion mid-jog during ordinary slow jogging.
enc, sched = new_scheduler()
ticks_between_detents = 250 // TICK_MS      # a slow but continuous 4 detents/s
slow_turn = []
for _ in range(6):
    enc.move(4)
    slow_turn.append(sched.tick())
    for _ in range(ticks_between_detents - 1):
        slow_turn.append(sched.tick())
check("slow continuous turn is never mistaken for a stop",
      [m for m in slow_turn if m and m["t"] == "jog_cancel"], [])
check("  and never triggers a cancel", sched.stats["cancels"], 0)

enc.move(4)
check("  and real motion still registers after dithering",
      motion(sched.tick()), {"t": "jog", "axis": "X", "det": 1, "step": 0.1})

print("\njog cancel")

enc, sched = new_scheduler()
enc.move(4)
sched.tick()
cancels = [sched.tick() for _ in range(IDLE_TICKS_BEFORE_CANCEL)]
check("cancel emitted once motion stops", cancels[-1], {"t": "jog_cancel"})
check("  and not before the idle threshold",
      cancels[:-1], [None] * (IDLE_TICKS_BEFORE_CANCEL - 1))

enc, sched = new_scheduler()
enc.move(4)
sched.tick()
for _ in range(IDLE_TICKS_BEFORE_CANCEL):
    sched.tick()
check("cancel is not repeated while idle",
      [sched.tick() for _ in range(5)], [None] * 5)

enc, sched = new_scheduler()
enc.move(4)
sched.tick()
pause = [sched.tick() for _ in range(IDLE_TICKS_BEFORE_CANCEL - 1)]
enc.move(4)
resumed = sched.tick()
check("brief pause mid-turn does not cancel",
      (pause, resumed["t"]), ([None] * (IDLE_TICKS_BEFORE_CANCEL - 1), "jog"))

print("\nqueue-and-execute mode")

enc, sched = new_scheduler()
sched.cancel_on_stop = False
enc.move(4 * 5)
first = sched.tick()
idle = [sched.tick() for _ in range(IDLE_TICKS_BEFORE_CANCEL + 5)]
check("detents still delivered with cancel disabled", first["det"], 5)
check("  and stopping emits no cancel", [m for m in idle if m], [])
check("  so nothing flushes the queued motion", sched.stats["cancels"], 0)

sched.cancel_on_stop = True
enc.move(4)
sched.tick()
resumed = [sched.tick() for _ in range(IDLE_TICKS_BEFORE_CANCEL)]
check("re-enabling restores cancel on stop", resumed[-1], {"t": "jog_cancel"})

print("\naxis and step")

enc, sched = new_scheduler()
check("axis change while idle needs no cancel", sched.set_axis("Y"), None)
enc.move(4)
check("  and subsequent jogs use the new axis", sched.tick()["axis"], "Y")

enc, sched = new_scheduler()
enc.move(4)
sched.tick()
check("axis change mid-motion cancels the old axis",
      sched.set_axis("Z"), {"t": "jog_cancel"})

enc, sched = new_scheduler()
enc.move(4)
sched.tick()
sched.set_axis("Z")
enc.move(4)
check("  and motion during the switch is discarded",
      motion(sched.tick()), {"t": "jog", "axis": "Z", "det": 1, "step": 0.1})

enc, sched = new_scheduler()
sched.step_up()
enc.move(4)
check("step up changes the step size", sched.tick()["step"], STEP_SIZES[3])

enc, sched = new_scheduler()
for _ in range(10):
    sched.step_down()
check("step size clamps at the bottom", sched.step, STEP_SIZES[0])
for _ in range(10):
    sched.step_up()
check("step size clamps at the top", sched.step, STEP_SIZES[-1])

print("\ndisabled")

enc, sched = new_scheduler()
sched.enabled = False
enc.move(4 * 10)
check("disabled scheduler emits nothing", sched.tick(), None)
sched.enabled = True
enc.move(4)
check("  and motion while disabled is discarded, not replayed",
      motion(sched.tick()), {"t": "jog", "axis": "X", "det": 1, "step": 0.1})

print()
if failures:
    print("{} FAILED: {}".format(len(failures), ", ".join(failures)))
    sys.exit(1)
print("all jog scheduler tests passed")
