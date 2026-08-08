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
                         FEED_MAX_MM_MIN, RATE_WINDOW_TICKS, TICK_MS,
                         QUEUE_TICKS, STEP_MAX_FEED, AXIS_MAX_FEED,
                         FEED_DEADBAND)

# Index by value, so adding a step to the ladder cannot silently retarget a
# test at a different step size.
COARSE = STEP_SIZES.index(1.0)
FINE = STEP_SIZES.index(0.01)

failures = []


def check(label, got, want, tol=None):
    # Feed is quantised by the deadband, so an exact comparison would be
    # asserting the deadband width rather than the behaviour under test.
    if tol is not None and isinstance(got, float) and isinstance(want, float):
        ok = abs(got - want) <= tol
    else:
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
# Rates chosen to stay inside what the step's feed ceiling can execute. Past
# that the queue bound deliberately drops, which the drop test below covers.
per_detent = []
for rate in (1, 2, 3):
    enc, sched = new_scheduler()
    for _ in range(RATE_WINDOW_TICKS * 2):
        enc.move(4 * rate)
        message = sched.tick()
    per_detent.append(abs(message["det"] * message["step"]) / rate)
check("distance per detent is identical at every turn speed",
      all(abs(d - 0.1) < 1e-9 for d in per_detent), True)

# Feed must never fall below the rate being commanded, or the shortfall queues
# every tick and runs on after the wheel stops.
enc, sched = new_scheduler()
sched.set_step_index(COARSE)             # 1.0 mm per detent
for _ in range(RATE_WINDOW_TICKS * 2):
    enc.move(4)                          # 1 detent per 20 ms = 50 detents/s
    tracked = sched.tick()
check("feed meets the commanded rate", tracked["feed"], 50 * 1.0 * 60, tol=1.0)

# Feed must not exceed the commanded rate. Over-feeding makes each move finish
# early and stop, so the planner accelerates and decelerates once per detent -
# which is what made fine steps violent on the machine.
enc, sched = new_scheduler()
for _ in range(RATE_WINDOW_TICKS * 2):
    enc.move(4 * 3)                      # 150 detents/s at 0.1 mm
    fine = sched.tick()
check("feed never exceeds the commanded rate", fine["feed"], 150 * 0.1 * 60, tol=1.0)

# Which means a move lasts about a full tick, so consecutive jogs join up
# instead of each one starting and stopping.
move_ms = (abs(fine["det"]) * fine["step"] / (fine["feed"] / 60.0)) * 1000
check("  so each move spans roughly a whole tick",
      abs(move_ms - TICK_MS) < TICK_MS * 0.5, True)

# Capped, because asking for more than the machine can deliver only rebuilds
# the queue this design exists to keep shallow.
enc, sched = new_scheduler()
sched.set_step_index(COARSE)
for _ in range(RATE_WINDOW_TICKS * 2):
    enc.move(4 * 6)                      # 300 detents/s at 1 mm
    capped = sched.tick()
check("feed is capped at the step's ceiling",
      capped["feed"], min(STEP_MAX_FEED[COARSE], AXIS_MAX_FEED["X"]), tol=5.0)

# Z is far slower than X and Y here, so the ceiling has to follow the axis.
enc, sched = new_scheduler(axis="Z")
sched.set_step_index(COARSE)
for _ in range(RATE_WINDOW_TICKS * 3):
    enc.move(4 * 6)
    z_capped = sched.tick()
check("Z is capped at its own lower ceiling",
      z_capped["feed"], AXIS_MAX_FEED["Z"], tol=5.0)

# Out-turning the machine must drop the surplus, not bank it. Banking is what
# made run-on grow the longer the pendant was used: every back-and-forth added
# more than the machine drained, and none of it paused long enough to cancel.
bound = (sched.feed / 60.0) * (TICK_MS / 1000.0) * QUEUE_TICKS
check("in-flight distance is bounded, so run-on cannot grow",
      sched._queue_mm <= bound + 1e-9, True)
check("  and the dropped detents are counted",
      sched.stats["dropped_detents"] > 0, True)

# Within what the machine can follow, nothing is dropped at all.
# 3 detents/tick at 0.1 mm asks for 900 mm/min, just inside the 950 ceiling.
enc, sched = new_scheduler()
for _ in range(RATE_WINDOW_TICKS * 2):
    enc.move(4 * 3)
    sched.tick()
check("nothing is dropped while the machine can keep up",
      sched.stats["dropped_detents"], 0)

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
check("fast turning raises the feed", spinning["feed"], STEP_MAX_FEED[2], tol=5.0)

for _ in range(RATE_WINDOW_TICKS):
    sched.tick()                         # idle; the window fills with zeros
enc.move(4)
decayed = sched.tick()["feed"]
check("  and it decays away while idle", decayed < FEED_MAX_MM_MIN / 10, True)

print("\nfeed steadiness")

# grblHAL blends consecutive moves that share a feed. A feed drifting every
# message forces a velocity change between every block, felt as jerking once up
# to speed - clamping the feed to a constant was smooth on the machine, which is
# what identified this. Smoothing does not help: it changes how fast the feed
# moves, not how often. A steady hand must produce a steady F, jitter and all.
enc, sched = new_scheduler()
sched.set_step_index(COARSE)                    # headroom below the ceiling, so
                                                # this tests the feed logic and
                                                # not the cap
feeds = []
wobble = (3, 3, 4, 3, 2, 3, 4, 3, 3, 2, 3, 4)   # a real hand is not metronomic
for _ in range(4):
    for detents in wobble:
        enc.move(4 * detents)
        message = sched.tick()
        if message and message["t"] == "jog":
            feeds.append(message["feed"])
settled = feeds[len(feeds) // 2:]               # ignore the opening ramp
check("a steady hand produces a steady feed", len(set(settled)), 1)

# It must still follow a genuine change of speed.
for _ in range(RATE_WINDOW_TICKS * 2):
    enc.move(4 * 9)
    faster = sched.tick()
check("  but still follows a real change in speed",
      faster["feed"] > settled[-1] * (1 + FEED_DEADBAND), True)

# Reported from the machine: after a stumble the feed stopped matching the hand
# and stayed rough. A symmetric band strands the feed above the target after any
# slowdown, and a feed above the commanded rate means every move finishes early
# and waits - the stutter, self-sustaining until the target falls far enough to
# leave the band.
enc, sched = new_scheduler()
sched.set_step_index(COARSE)
for _ in range(RATE_WINDOW_TICKS * 2):
    enc.move(4 * 5)                      # 250 detents/s
    quick = sched.tick()
for _ in range(RATE_WINDOW_TICKS * 3):
    enc.move(4 * 2)                      # slowed to 100 detents/s
    slowed = sched.tick()
commanded = 100 * STEP_SIZES[COARSE] * 60
check("feed follows a slowdown rather than stranding high",
      slowed["feed"] <= commanded * (1 + FEED_DEADBAND) + 1, True)
check("  having actually come down from the quick feed",
      slowed["feed"] < quick["feed"], True)

# Climbing towards a ceiling must actually arrive at it. Easing towards a target
# from inside a deadband strands the feed short: each step shrinks until the
# remaining gap fits in the band, and there it stops - persistently below the
# rate being commanded, which grows the queue and brings the jerking back.
enc, sched = new_scheduler()
sched.set_step_index(COARSE)
for _ in range(RATE_WINDOW_TICKS * 4):
    enc.move(4 * 6)                      # demands well past the ceiling
    pinned = sched.tick()
ceiling = min(STEP_MAX_FEED[COARSE], AXIS_MAX_FEED["X"])
check("feed reaches its ceiling rather than stalling short",
      pinned["feed"], ceiling, tol=1.0)

# Reported from the machine: a dead stop, a slight pause, then a ramp back up,
# about three times across a 49 inch traverse and twice as often at 0.5 mm.
#
# That is the jog cancel firing while the operator re-grips the wheel. A
# traverse needs 12 revolutions at 1 mm and 25 at 0.5 mm, which cannot be turned
# without letting go, and each pause past the threshold flushed and stopped the
# machine. The 2:1 ratio of occurrences matches the 2:1 ratio of revolutions.
enc, sched = new_scheduler()
sched.set_step_index(STEP_SIZES.index(0.5))
for _ in range(RATE_WINDOW_TICKS * 2):
    enc.move(4 * 6)
    sched.tick()
regrip = [sched.tick() for _ in range(1000 // TICK_MS)]   # a second of stillness
check("a pause to re-grip the wheel does not cancel",
      [m for m in regrip if m], [])

# A genuine stop still cancels, just later - and once, somewhere in the tail
# rather than at its end, since the re-grip ticks already counted towards it.
tail = [sched.tick() for _ in range(IDLE_TICKS_BEFORE_CANCEL)]
check("  while a real stop still cancels",
      [m for m in tail if m], [{"t": "jog_cancel"}])

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
