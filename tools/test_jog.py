"""Tests for the jog scheduler. Runs on a PC with a fake encoder.

    python tools/test_jog.py
"""

import random
import sys
import types
from pathlib import Path

sys.modules.setdefault("machine", types.ModuleType("machine"))

# MicroPython's monotonic tick helpers, which the scheduler uses to measure how
# long a tick really takes. The deltas between calls here are microseconds, far
# below TICK_MS, so the measurement's own guard rejects them and the nominal
# period stands - which is what keeps every timing expectation below valid.
import time  # noqa: E402

time.ticks_ms = lambda: int(time.monotonic() * 1000)
time.ticks_add = lambda t, d: t + d
time.ticks_diff = lambda a, b: a - b
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "micropython"))

from pendant import protocol  # noqa: E402
from pendant.jog import (JogScheduler, STEP_SIZES,  # noqa: E402
                         IDLE_TICKS_BEFORE_CANCEL, FEED_MIN_MM_MIN,
                         FEED_MAX_MM_MIN, RATE_WINDOW_TICKS, TICK_MS,
                         RATE_ATTACK_TICKS, RATE_ATTACK_MARGIN,
                         RATE_ATTACK_HOLD_TICKS,
                         COUNTS_PER_DETENT,
                         STEP_MAX_FEED, AXIS_MAX_FEED,
                         FEED_DEADBAND,
                         FEED_BANDS, FEED_RISE_BAND_STEPS,
                         FEED_FALL_BAND_STEPS,
                         PLANNER_TARGET_BLOCKS, PLANNER_FILL_RATIO,
                         RUNAHEAD_LIMIT_S, MIN_RUNAHEAD_MM,
                         MIN_FEED_BLOCK_MS)

# Index by value, so adding a step to the ladder cannot silently retarget a
# test at a different step size.
COARSE = STEP_SIZES.index(1.0)
FINE = STEP_SIZES.index(0.01)
# Between the two, for cases that need room under the ceiling or finer
# granularity than whole detents at a coarse step can give.
MID = STEP_SIZES.index(0.1)

# Long enough for the rate window to fill, so these measure the settled feed
# rather than the transient at the start of a burst.
SETTLE = RATE_WINDOW_TICKS * 6

def feed_for(detents_per_tick, step):
    """Feed the scheduler should command for a given arrival rate."""
    return (detents_per_tick / (TICK_MS / 1000.0)) * step * 60.0


def near(expected):
    """Tolerance for a settled feed.

    The feed is not pinned to the arrival rate: the deadband lets it sit up to
    10% away before it follows a change. Asserting equality would be asserting
    the deadband width rather than the behaviour under test.
    """
    return expected * 0.12


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

# Feed bands round DOWN, so the emission budget is deliberately under the
# arrival rate and a fast tick sheds its surplus - see FEED_BANDS. What is
# under test here is coalescing: many detents in a tick produce ONE message
# carrying as many as the band allows, never a message each.
enc, sched = new_scheduler()
enc.move(4 * 7)
coalesced = motion(sched.tick())
check("seven detents in one tick coalesce into one message",
      (coalesced["t"], coalesced["axis"], coalesced["step"]), ("jog", "X", 0.1))
check("  carrying what the band allows, and no more than arrived",
      1 < coalesced["det"] <= 7, True)

enc, sched = new_scheduler()
check("no movement produces no message", sched.tick(), None)

enc, sched = new_scheduler()
enc.move(-4 * 3)
reversed_move = motion(sched.tick())
check("reverse rotation gives negative detents",
      reversed_move["det"] < 0, True)
check("  and never more travel than the wheel produced",
      abs(reversed_move["det"]) <= 3, True)

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

# The contract, as it now stands. Every detent that is EMITTED carries exactly
# one step - that is what keeps this end, the sender and the controller
# agreeing about where the axis is, and nothing may scale it.
#
# What is no longer promised is that every detent is emitted. Feed bands round
# down, so the wheel is a throttle rather than a position source: turning
# faster selects a higher band, and the surplus above what that band executes
# is dropped. Measured at 24-41% of wheel travel across the bands.
#
# That was a deliberate trade and it is worth restating why, because the
# comment this replaces argued the opposite. A constant F and exact distance
# tracking of a varying hand cannot both hold - the only feed that tracks
# distance exactly is one that changes on every block, and grblHAL is
# trapezoidal with no S-curve, so a changing F is a decelerate and accelerate
# every time. The machine showed the cost as a feed that searched constantly
# and, at 1 mm, pinned at its ceiling. Distance fidelity was what got spent.
per_detent = []
for rate in (1, 2, 3):
    enc, sched = new_scheduler()
    for _ in range(SETTLE):
        enc.move(4 * rate)
        message = sched.tick()
    per_detent.append(abs(message["step"]))
check("every emitted detent carries exactly one step",
      all(abs(d - 0.1) < 1e-9 for d in per_detent), True)

# And never more distance than the wheel actually produced. Dropping is the
# permitted direction; inventing motion is not.
enc, sched = new_scheduler()
produced = 0
emitted = 0.0
for _ in range(SETTLE):
    enc.move(4 * 3)
    produced += 3
    message = sched.tick()
    if message:
        emitted += abs(message["det"]) * message["step"]
check("  and the axis is never sent further than the wheel turned",
      emitted <= produced * 0.1 + 1e-9, True)

# Feed must not exceed what the hand is supplying. Over-feeding is the starve
# condition: the machine cannot be fed at a rate the wheel is not producing, so
# each move finishes early, the planner empties and the controller decelerates
# into every block. The machine showed it as F8000/act2666 holding one block.
#
# Rounding bands down is what guarantees this, and it is the direction the old
# nearest-rounding grid got wrong - measured commanding above supply on 40 to
# 73% of blocks against 0 to 13% here.
for step_index, rate in ((COARSE, 1), (MID, 3)):
    enc, sched = new_scheduler()
    sched.set_step_index(step_index)
    for _ in range(SETTLE):
        enc.move(4 * rate)
        tracked = sched.tick()
    asked = feed_for(rate, STEP_SIZES[step_index])
    check("{0} mm never commands above what the hand supplies".format(
        STEP_SIZES[step_index]), tracked["feed"] <= asked * 1.02, True)
    # But within a band of it, or the wheel would feel dead. A band is the
    # ease-off room the operator gets: anywhere inside one holds the same F.
    _, prober = new_scheduler()
    prober.set_step_index(step_index)
    band = prober._quantum(STEP_MAX_FEED[step_index])
    check("  and stays within one band of it",
          tracked["feed"] >= min(asked, prober.feed_floor()) - band, True)

# Which means a move lasts about a full tick, so consecutive jogs join up
# instead of each one starting and stopping. Still true under banding: the
# emission budget is derived from the commanded feed, so however far down a
# band rounds, what goes out is still a tick's worth of it. `tracked` is the
# last message from the loop above, at the mid step.
move_ms = (abs(tracked["det"]) * tracked["step"]
           / (tracked["feed"] / 60.0)) * 1000
check("  so each move spans roughly a whole tick",
      abs(move_ms - TICK_MS) < TICK_MS * 0.5, True)

# Capped, because asking for more than the machine can deliver only rebuilds
# the queue this design exists to keep shallow.
enc, sched = new_scheduler()
sched.set_step_index(COARSE)
for _ in range(SETTLE):
    enc.move(4 * 40)          # well past what the ceiling allows
    capped = sched.tick()
# The commanded feed now sits exactly on the ceiling. It used to be allowed a
# few percent under, because a queue-model trim shaded it while that model
# thought the buffer was filling - a second regulator, since retired.
ceiling_x = min(STEP_MAX_FEED[COARSE], AXIS_MAX_FEED["X"])
check("feed is capped at the step's ceiling",
      abs(capped["feed"] - ceiling_x) <= 1, True)

# Z is far slower than X and Y here, so the ceiling has to follow the axis.
enc, sched = new_scheduler(axis="Z")
sched.set_step_index(COARSE)
for _ in range(SETTLE):
    enc.move(4 * 40)
    z_capped = sched.tick()
check("Z is capped at its own lower ceiling",
      AXIS_MAX_FEED["Z"] - 1 <= z_capped["feed"]
      <= AXIS_MAX_FEED["Z"] + 1, True)

# Out-turning the machine must drop the surplus, not bank it. Banking is what
# made run-on grow the longer the pendant was used: every back-and-forth added
# more than the machine drained, and none of it paused long enough to cancel.
#
# What bounds it now is the emission cap: a tick may carry at most what the
# commanded feed drains, plus whatever the depth regulator is adding to reach
# its target. Everything past that is dropped rather than queued.
per_tick_drain = (sched.feed / 60.0) * (TICK_MS / 1000.0)
check("in-flight distance is bounded, so run-on cannot grow",
      sched._queue_mm <= per_tick_drain * PLANNER_TARGET_BLOCKS
      * PLANNER_FILL_RATIO * 2, True)
check("  and the dropped detents are counted",
      sched.stats["dropped_detents"] > 0, True)

# Under the first band the feed still tracks the hand exactly, so nothing is
# dropped there. Banding only sheds surplus once it is rounding down, which is
# traverse speed - at 1 mm the first band is 33 detents/s, so every real
# traverse bands while ordinary fine work does not.
#
# 2 detents/tick at 0.1 mm asks 600 mm/min, just under the 625 first band.
enc, sched = new_scheduler()
for _ in range(RATE_WINDOW_TICKS * 2):
    enc.move(4 * 2)
    sched.tick()
check("nothing is dropped below the first band",
      sched.stats["dropped_detents"], 0)

enc, sched = new_scheduler()
sched.feed_tracking = False
enc.move(4)
check("feed tracking can be disabled", sched.tick()["feed"], FEED_MAX_MM_MIN)

# A pause must not leave a high feed armed for the next careful detent - that
# would make the first move after a pause far faster than intended.
enc, sched = new_scheduler()
for _ in range(SETTLE):
    enc.move(4 * 6)
    spinning = sched.tick()
# Banded, so not the hand's rate exactly - the band at or below it. What must
# hold is that turning faster commands more, and never more than was supplied.
_, band_probe = new_scheduler()
band_probe.set_step_index(2)
one_band = band_probe._quantum(STEP_MAX_FEED[2])
check("fast turning raises the feed",
      spinning["feed"] > band_probe.feed_floor() * 2, True)
check("  to the band at or below the hand, never above it",
      feed_for(6, 0.1) - one_band <= spinning["feed"] <= feed_for(6, 0.1) * 1.02,
      True)

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
sched.set_step_index(MID)                       # headroom below the ceiling, so
                                                # this tests the feed logic and
                                                # not the cap. The coarse step
                                                # no longer has any: this wobble
                                                # is ~150 detents/s, which at
                                                # 1 mm asks 9000 against a
                                                # ceiling of 8000, so every
                                                # sample would be the cap.
feeds = []
wobble = (3, 3, 4, 3, 2, 3, 4, 3, 3, 2, 3, 4)   # a real hand is not metronomic
for _ in range(8):
    for detents in wobble:
        enc.move(4 * detents)
        message = sched.tick()
        if message and message["t"] == "jog":
            feeds.append(message["feed"])
settled = feeds[len(feeds) // 2:]               # ignore the opening ramp
# The feed is no longer a single value: it is trimmed a few percent while the
# planner buffer refills, and that trim toggles as the buffer crosses its
# target. What matters is that the spread stays small - a few percent is a
# velocity change the machine absorbs in milliseconds, where the tens of
# percent it used to swing by is what stopped grblHAL blending.
# Two things move the commanded feed by design: the deadband, which lets it sit
# up to its width from the target before following, and the buffer trim. Their
# sum is the most a steady hand should ever produce - against the tens of
# percent it swung by when it was following every jitter.
spread = (max(settled) - min(settled)) / max(settled)
check("a steady hand produces a nearly steady feed",
      spread <= FEED_DEADBAND + 0.02, True)

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
for _ in range(SETTLE):
    enc.move(4 * 40)                     # demands well past the ceiling
    pinned = sched.tick()
ceiling = min(STEP_MAX_FEED[COARSE], AXIS_MAX_FEED["X"])
check("feed reaches its ceiling rather than stalling short",
      abs(pinned["feed"] - ceiling) <= 1, True)

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

print("\nstarting from rest")


def per_tick(detents_per_second):
    """Detents landing in one tick at a given turn rate.

    Rates are stated per second, not per tick, so they keep their meaning when
    the tick rate changes. Stated per tick, a "gentle" six became 300 detents/s
    when the tick shortened to 20 ms - fast enough to pin the feed ceiling, so
    gentle and hard turned into the same test.
    """
    return max(1, int(detents_per_second * TICK_MS / 1000.0 + 0.5))


def first_feed(detents_per_second, step=0.5, idle=40):
    """Feed commanded by the opening tick of a burst after a pause."""
    enc, sched = new_scheduler()
    for _ in range(STEP_SIZES.index(step) - STEP_SIZES.index(0.1)):
        sched.step_up()
    for _ in range(idle):
        sched.tick()
    enc.move(4 * per_tick(detents_per_second))
    message = sched.tick()
    return message["feed"] if message else 0


# The opening tick has to match the hand. Dividing the first detents by the
# whole idle gap - or averaging them against a window still full of idle zeros -
# reports a fraction of the real turn rate, so a burst opens at the floor and
# climbs for nine ticks. On the machine that is having to spin the wheel a while
# before anything happens.
fast = first_feed(280)          # a hard wind
# Against the ceiling rather than a literal. 280 detents/s quantises to 6 per
# tick, which demands 9000 at 0.5 mm - more than the step is allowed - so what
# "full speed" means here is the ceiling, whatever it currently is. The bare
# 8000 that used to stand here was chosen when that ceiling was 9000, and it
# failed the moment the ceiling was trimmed to buy back run-off: a lower
# ceiling reported as a feed-tracking regression, which is not what this
# checks.
opening_target = min(feed_for(per_tick(280), 0.5),
                     STEP_MAX_FEED[STEP_SIZES.index(0.5)])
check("a hard start reaches speed on the first tick",
      fast >= opening_target * 0.95, True)
check("  and a gentle one is proportional, not full speed",
      FEED_MIN_MM_MIN < first_feed(100) < fast, True)

# The other half of the same trade. A single click is a deliberate nudge for
# fine positioning and the idle gap is the only evidence of its speed, so it
# has to stay slow or fine work turns twitchy.
# One detent in the opening tick carries no speed information beyond the gap it
# arrived in, so it stays at the floor however long the pause was. That is the
# deliberate nudge case, and it is why the rate above needs two.
_, floor_probe = new_scheduler()
floor_probe.set_step_index(STEP_SIZES.index(0.5))
check("  while a lone click stays at the floor",
      first_feed(1), floor_probe.feed_floor())

# The floor is scaled so that click cannot hold the pipeline open. A fixed
# 50 mm/min is 120 ms of block at 0.1 mm but 600 ms at 0.5 mm, and the machine
# crawled through it at a reported feed of 50 against 8550 commanded while
# seventeen blocks piled up behind and the commanded position ran 135 mm ahead.
worst_block_ms = 0.0
for index in range(len(STEP_SIZES)):
    _, probe = new_scheduler()
    probe.set_step_index(index)
    block_ms = STEP_SIZES[index] / (probe.feed_floor() / 60.0) * 1000.0
    if block_ms > worst_block_ms:
        worst_block_ms = block_ms
check("  and no step lets one detent hold the pipeline open",
      worst_block_ms <= MIN_FEED_BLOCK_MS + 1, True)

# A pause mid-turn must still decay the feed - which is why idle ticks enter the
# window at all. Only a start from rest clears it.
enc, sched = new_scheduler()
for _ in range(12):
    enc.move(4 * 8)
    sched.tick()
turning = sched.feed
for _ in range(6):
    sched.tick()
check("  and a pause mid-turn still lowers the feed", sched.feed < turning, True)

import io  # noqa: E402
from pendant import jog as _jog  # noqa: E402

# Capacity has to be learned the moment a count arrives, not when a jog is
# emitted. A pendant sitting still emits nothing, so learning it at emission
# left capacity reading zero while perfectly good counts came in - and zero
# capacity is how "no Bf: report at all" is detected, so every session began by
# announcing a fault that was not there.
enc, sched = new_scheduler()
sched.set_planner_free(128)
check("capacity is learned without any motion", sched.planner_capacity, 128)
check("  and free is recorded with it", sched.planner_free, 128)

sched.set_planner_free(120)
check("  a lower count does not lower capacity", sched.planner_capacity, 128)
check("    but is recorded as the current depth", sched.planner_free, 120)

enc, sched = new_scheduler()
check("no report leaves capacity at zero", sched.planner_capacity, 0)


print("\ndiagnostic output")


def dump_lines(budget, dumps=20):
    """Lines printed by that many dumps at a given budget."""
    saved = _jog.TRACE_DUMP_BUDGET
    _jog.TRACE_DUMP_BUDGET = budget
    try:
        enc, sched = new_scheduler()
        for _ in range(RATE_WINDOW_TICKS * 2):
            enc.move(4 * 3)
            sched.tick()
        buffer = io.StringIO()
        stdout, sys.stdout = sys.stdout, buffer
        try:
            for index in range(dumps):
                sched.dump_trace("test {}".format(index))
        finally:
            sys.stdout = stdout
        return len(buffer.getvalue().splitlines())
    finally:
        _jog.TRACE_DUMP_BUDGET = saved


# Printing is what stalls the event loop: a blocked print on USB CDC blocks the
# socket read and the jog scheduler with it. The budget has to actually bound
# the output, not merely thin it.
budgeted = dump_lines(6)
check("the budget bounds what twenty dumps print", budgeted < 200, True)
check("  and zero silences them entirely", dump_lines(0), 0)
check("    while still counting", _jog.TRACE_DUMP_BUDGET >= 0, True)


print("\nplanner depth regulation")

# The controller reports free planner slots. Emission runs above the drain rate
# while the planner is shallow and settles back to it once supplied, which is
# what stops a transport hiccup from emptying the buffer and stalling the axis.
CAPACITY = 128

def run_at_depth(free, ticks=SETTLE, detents_per_tick=40, step_index=COARSE,
                 lag_mm=0.0, actual_feed=None):
    """Drive a steady turn while the controller reports a fixed depth.

    `actual_feed` is what the controller reports draining. None stands for a
    machine keeping up exactly with the commanded feed, which is what every
    case here assumed before the recovery path began regulating against it.
    """
    enc, sched = new_scheduler()
    # Set, not stepped. This walked step_up() step_index times from whatever the
    # scheduler starts on - which is 0.1 mm, not the finest - so the argument
    # was a repeat count wearing an index's name. COARSE worked only because
    # four step-ups saturate the ladder anyway, and every other value landed on
    # 1 mm too.
    sched.set_step_index(step_index)
    sched.planner_capacity = CAPACITY
    sched.planner_free = free
    sent = 0
    for _ in range(ticks):
        sched.lag_mm = lag_mm
        sched.actual_feed = sched.feed if actual_feed is None else actual_feed
        enc.move(4 * detents_per_tick)
        message = motion(sched.tick())
        if message:
            sent += abs(message["det"])
    return sched, sent

# Shallow: the pendant must send more than the machine drains, or depth can
# never build. This is the case that was broken - emission exactly matched the
# drain, so the planner sat two to four blocks deep and any stall emptied it.
# Measured at MID rather than COARSE, because emission is a whole number of
# detents per tick and at a coarse step that number is small: the 1 mm ceiling
# gives (8000/60) x 20 ms / 1 mm = 2.7, so rounding moves the measured ratio by
# a fifth either way and the reading says more about truncation than about the
# fill. A finer step makes the same quantity ~8 detents, where rounding is
# noise. The behaviour under test is identical; only the resolution changes.
starved, sent_starved = run_at_depth(CAPACITY, step_index=MID)
supplied, sent_supplied = run_at_depth(CAPACITY - PLANNER_TARGET_BLOCKS,
                                       step_index=MID)
check("an empty planner is fed faster than it drains", sent_starved > sent_supplied, True)
# Not the exact ratio: a tick carries a whole number of detents, so the cap
# truncates, and the older build trim still shades the feed while the queue
# model fills. Both push the measured figure above the nominal one. What
# matters is that filling is substantially faster than draining and not so
# fast it overshoots the planner.
ratio = sent_starved / max(sent_supplied, 1)
check("  and a supplied one is fed at the drain rate",
      PLANNER_FILL_RATIO * 0.85 < ratio < PLANNER_FILL_RATIO * 1.25, True)

# Run-ahead is bounded independently of depth. Without this the fill runs
# whenever the planner is shallow, and at a coarse step the planner is
# permanently shallow - so it never stops, and the surplus becomes lag rather
# than depth. The machine measured 96 mm behind the hand before this bound.
starved_far_behind, sent_far = run_at_depth(CAPACITY, lag_mm=10000.0,
                                            step_index=MID)
check("run-ahead past the limit stops the fill",
      sent_far <= sent_supplied, True)

# Stopping the fill is not enough on its own - the branch that stops it also
# has to be able to get back. Pinned at the commanded feed it could not: the
# hand sets that, so a machine falling behind was still sent the hand's rate,
# commanded distance outran actual, and the lag that triggered the bound could
# only grow from there. Measured on the machine as 521 mm with the planner
# never once leaving its maximum, against a replay of the identical stream
# from the PC that ran smooth and filled it a hundred blocks deep.
_, sent_slow = run_at_depth(CAPACITY, lag_mm=10000.0, actual_feed=1200.0)
_, sent_quick = run_at_depth(CAPACITY, lag_mm=10000.0, actual_feed=9000.0)
check("  and past it emission follows the machine, not the hand",
      sent_slow < sent_quick, True)

# Zero is what an idle or stale report reads. Stopping dead on it strands the
# lag exactly as pinning at the hand's rate did, one direction round instead of
# the other, so the floor matters as much as the cap.
_, sent_idle = run_at_depth(CAPACITY, lag_mm=10000.0, actual_feed=0.0)
check("    while a zero report still emits", sent_idle > 0, True)
# The bound has to clear the baseline transport lag, which at a fine step is
# larger than a quarter second of travel. Sized only on time it fired before
# any filling had happened and held the planner empty.
fine_lag = 17.0                      # measured at 0.1 mm with one block held
fine_feed = 2375.0
check("  and clears baseline transport lag at a fine step",
      max((fine_feed / 60.0) * RUNAHEAD_LIMIT_S, MIN_RUNAHEAD_MM) > fine_lag, True)

# While still binding where run-ahead actually runs away.
check("  while still binding at a coarse step",
      max((9000.0 / 60.0) * RUNAHEAD_LIMIT_S, MIN_RUNAHEAD_MM) < 96.0, True)


print("\nclosed loop: emission against a machine that drains")

# Every check above hands the scheduler a lag_mm and asks what it emits. None
# of them close the loop the other way round - lag is a *consequence* of
# emission, and that half was never covered. It is why the 1 mm run-on survived
# three rounds of hardware testing and three failed explanations: each theory
# was tested against a fixture that could not express the fault.
#
# The machine modelled here executes what it was commanded at the commanded
# feed and no faster, and hands its backlog back as lag and planner depth.


def closed_loop(step, seconds=6.0, tick_ms=27.0, overdrive=1.15):
    """A steady saturating turn. Returns (worst lag, commanded, executed)."""
    enc, sched = new_scheduler()
    sched.set_step_index(STEP_SIZES.index(step))
    sched.planner_capacity = CAPACITY
    sched.planner_free = CAPACITY
    # Deltas in this harness are microseconds, so the measurement guard rejects
    # them and whatever is set here stands. 27 ms is the period measured on the
    # board against a nominal 20.
    sched._tick_ms = tick_ms
    dt = tick_ms / 1000.0
    # Fast enough to sit on the step's ceiling, which is where the fault lives.
    # Below it the commanded feed follows the hand, so emission and drain are
    # the same quantity and no surplus exists to accumulate.
    ceiling = STEP_MAX_FEED[STEP_SIZES.index(step)]
    detents_per_s = (ceiling / 60.0 / step) * overdrive
    queued = commanded = executed = worst = carry = 0.0
    for _ in range(int(seconds / dt)):
        moved = min(queued, (sched.feed / 60.0) * dt)
        queued -= moved
        executed += moved
        if queued > worst:
            worst = queued
        sched.lag_mm = queued
        sched.actual_feed = (moved / dt) * 60.0
        held = min(CAPACITY, int(queued / step + 0.5))
        sched.planner_free = CAPACITY - held
        carry += detents_per_s * dt * COUNTS_PER_DETENT
        whole = int(carry)
        carry -= whole
        enc.move(whole)
        message = motion(sched.tick())
        if message:
            travelled = abs(message["det"]) * message["step"]
            queued += travelled
            commanded += travelled
    return worst, commanded, executed


# The fault in one line: at 1 mm the ceiling drains (8000/60) x 27 ms = 3.6
# detents a tick, and rounding the budget to whole detents granted 4. That is
# 11% more distance than the machine can execute, every tick, for as long as
# the turn is held - 15 mm/s of lag, and 90 mm in a six second burst, which is
# what the machine measured.
for step in (0.1, 0.5, 1.0):
    worst, commanded, executed = closed_loop(step)
    bound = max((STEP_MAX_FEED[STEP_SIZES.index(step)] / 60.0)
                * RUNAHEAD_LIMIT_S, MIN_RUNAHEAD_MM)
    check("{0} mm stays inside its own run-ahead bound".format(step),
          worst <= bound, True)
    check("  and commands no more than the machine executes",
          commanded <= executed * 1.04, True)

# Both tick periods. The bug is a rounding boundary, so which side of it a step
# lands on moves with the tick - 0.5 mm rounds down at 27 ms and lands exactly
# on the boundary at 20, and a measured period that drifts must not hand the
# fault to a different step.
for tick_ms in (20.0, 27.0):
    for step in (0.5, 1.0):
        _, commanded, executed = closed_loop(step, tick_ms=tick_ms)
        check("{0} mm at a {1:.0f} ms tick does not outrun the machine"
              .format(step, tick_ms), commanded <= executed * 1.04, True)


print("")
print("feed bands")

# Quantising the feed moved here from the sender, where it was four
# configuration knobs. The pendant throttles its own emission against the feed
# it believes it commanded, so while the sender did the snapping that belief
# was wrong by up to a whole grid step - two places deciding one number.


def steady_turn(step, fraction, seconds=4.0, tick_ms=27.0, wobble=0.12,
                seed=1):
    """A hand holding a speed, imperfectly. Returns the feeds commanded."""
    rnd = random.Random(seed)
    enc, sched = new_scheduler()
    sched.set_step_index(STEP_SIZES.index(step))
    sched._tick_ms = tick_ms
    sched.planner_capacity = CAPACITY
    sched.planner_free = CAPACITY - PLANNER_TARGET_BLOCKS
    ceiling = STEP_MAX_FEED[STEP_SIZES.index(step)]
    detents_per_s = (ceiling / 60.0 / step) * fraction
    dt = tick_ms / 1000.0
    feeds = []
    carry = 0.0
    for _ in range(int(seconds / dt)):
        carry += detents_per_s * (1.0 + rnd.uniform(-wobble, wobble))             * dt * COUNTS_PER_DETENT
        whole = int(carry)
        carry -= whole
        enc.move(whole)
        message = sched.tick()
        if message and message.get("t") == "jog":
            feeds.append(message["feed"])
    return feeds


# Every commanded feed sits on the step's own grid. The grid scales with the
# step because feed is turn_rate x step x 60, so the same hand wobble moves the
# feed twice as far at 1 mm as at 0.5 - a flat grid is right at one step only.
def quantum_for(step):
    """The band width the scheduler actually uses at this step."""
    _, probe = new_scheduler()
    probe.set_step_index(STEP_SIZES.index(step))
    return probe._quantum(STEP_MAX_FEED[STEP_SIZES.index(step)])


off_grid = []
for step in (0.1, 0.5, 1.0):
    quantum = quantum_for(step)
    for fraction in (0.45, 0.95):
        for feed in steady_turn(step, fraction):
            # To wire precision: protocol.jog rounds the feed to a decimal
            # place, so a grid that does not divide evenly - 8000/12 at 1 mm -
            # lands a tenth away from the exact multiple.
            nearest = round(feed / quantum) * quantum
            if abs(feed - nearest) > 0.05:
                off_grid.append((step, feed))
check("every commanded feed lands on one of the step's bands", off_grid, [])

# Rounded DOWN, never up. Inside a band the hand must always be supplying at
# least what was commanded - the opposite is the starve condition, and it is
# what rounding to nearest produced on 40 to 73% of blocks.
for step in (0.5, 1.0):
    ceiling = STEP_MAX_FEED[STEP_SIZES.index(step)]
    band = quantum_for(step)
    _, prober = new_scheduler()
    prober.set_step_index(STEP_SIZES.index(step))
    over = []
    for k in range(1, 40):
        want = ceiling * k / 40.0
        got = prober._snap(want, ceiling)
        if got > want and got > prober.feed_floor():
            over.append((want, got))
    check("{0} mm bands never round a feed upward".format(step), over, [])
    check("  and the top band is the ceiling itself",
          prober._snap(ceiling, ceiling), ceiling)

# The point of the grid. grblHAL is trapezoidal with junction deviation, so
# every change of F is a full decelerate and accelerate - a feed tracking the
# hand continuously puts a different F on every block and none of them chain.
for step in (0.5, 1.0):
    feeds = steady_turn(step, 0.95)
    changes = sum(1 for a, b in zip(feeds, feeds[1:]) if a != b)
    check("{0} mm holds its F word through hand wobble".format(step),
          changes <= len(feeds) // 20, True)

# The ceiling is never rounded through. Rounding to nearest can lift a feed
# past it, and this is the fight the sender could not win from outside: it had
# to snap up onto the grid, discover it had passed a ceiling it did not own,
# and snap back down - which reintroduced the off-grid values the grid existed
# to remove.
for step in (0.1, 0.5, 1.0):
    ceiling = STEP_MAX_FEED[STEP_SIZES.index(step)]
    check("{0} mm never commands above its ceiling".format(step),
          max(steady_turn(step, 1.4)) <= ceiling, True)

# And the top grid value is reachable, which the rise band exists to protect:
# the highest value on the grid *is* the ceiling, so getting there needs the
# request within half a grid step of full wheel speed.
for step in (0.5, 1.0):
    ceiling = STEP_MAX_FEED[STEP_SIZES.index(step)]
    check("  and {0} mm can still reach it".format(step),
          max(steady_turn(step, 1.05)) == ceiling, True)

# The floor is not quantised. It is already a constant, which is the whole of
# what the grid is for, and rounding 300 up to 500 at 0.5 mm would be the
# ceiling mistake at the other end - a bound the grid does not own being
# rounded through. The sender floored at a whole grid step because from outside
# it could not ask what the floor was.
_, probe = new_scheduler()
probe.set_step_index(STEP_SIZES.index(0.5))
check("a deliberate single click stays at the floor, not the grid",
      first_feed(1), probe.feed_floor())

# Falling out of the band is deliberately harder than rising. A commanded feed
# dropping below what the hand is supplying is what starves the planner, and a
# starve is felt as the jerk to zero and back.
check("the fall band is wider than the rise band",
      FEED_FALL_BAND_STEPS > FEED_RISE_BAND_STEPS, True)

# The attack window must not fire on noise. Detents arrive unevenly, so a
# five-tick window clumps well above its own mean as a matter of course, and
# deciding tick by tick turned that into feed. Measured with Poisson arrivals:
# a steady 4800 mm/min hand at 1 mm was commanded a median 5333 and spiked to
# the 8000 ceiling on 6% of blocks. The feed then sits above what the hand
# supplies, which is the starve condition - the machine reported F8000/act2666
# holding one block.
check("the attack window needs persistence, not one tick",
      RATE_ATTACK_HOLD_TICKS >= 2, True)
check("  and a margin clear of a window's own spread",
      RATE_ATTACK_MARGIN >= 1.5, True)

# A steady hand must not be commanded above what it is turning. This is the
# whole complaint - "1 mm just always searches until clamped at 8000" - and it
# is the one property no per-tick check can see.
def poisson(rnd, expect):
    limit = pow(2.718281828, -expect)
    product = 1.0
    count = 0
    while product > limit and count <= 60:
        product *= rnd.random()
        if product <= limit:
            return count
        count += 1
    return count


def steady_poisson(step, fraction, seconds=14.0, tick_ms=28.0, seed=11):
    """Detents arriving the way a hand produces them, at a constant mean."""
    rnd = random.Random(seed)
    enc, sched = new_scheduler()
    sched.set_step_index(STEP_SIZES.index(step))
    sched._tick_ms = tick_ms
    sched.planner_capacity = CAPACITY
    sched.planner_free = CAPACITY - PLANNER_TARGET_BLOCKS
    ceiling = STEP_MAX_FEED[STEP_SIZES.index(step)]
    detents_per_s = (ceiling / 60.0 / step) * fraction
    dt = tick_ms / 1000.0
    feeds = []
    for _ in range(int(seconds / dt)):
        enc.move(COUNTS_PER_DETENT * poisson(rnd, detents_per_s * dt))
        message = sched.tick()
        if message and message.get("t") == "jog":
            feeds.append(message["feed"])
    return feeds[len(feeds) // 3:], detents_per_s * step * 60.0, ceiling


for step in (0.5, 1.0):
    for fraction in (0.4, 0.6):
        feeds, hand, ceiling = steady_poisson(step, fraction)
        at_ceiling = sum(1 for v in feeds if v >= ceiling)
        check("{0} mm holding {1:.0f} never pins at its ceiling".format(step, hand),
              at_ceiling == 0, True)
        ordered = sorted(feeds)
        check("  and its median tracks the hand, not above it",
              ordered[len(ordered) // 2] <= hand * 1.10, True)

# But neither may exceed a whole grid step, and this is a property of the grid
# rather than a preference. Leaving the band puts the target more than the
# band's width from the held value and the snap rounds to nearest, so a band
# wider than a step lands two values away - and the value in between can never
# be commanded at all. At 1.5 the feed descended in double steps and sat
# stranded a step high between them, reported from the machine as being unable
# to hold a reduced feed at 1 mm: "only stable at the ceiling".
check("no hysteresis band reaches a whole feed band",
      max(FEED_RISE_BAND_STEPS, FEED_FALL_BAND_STEPS) < 1.0, True)

# Stranded high is the starve case, not the cure for it - the condition is the
# commanded feed exceeding what the hand supplies. Coming down off the ceiling
# must therefore land on the very next value, not skip one.
_, faller = new_scheduler()
faller.set_step_index(COARSE)
for _ in range(RATE_WINDOW_TICKS * 2):
    faller.encoder.move(4 * 5)
    faller.tick()
top = faller.feed
for _ in range(RATE_WINDOW_TICKS * 3):
    faller.encoder.move(4 * 2)
    faller.tick()
step_size = quantum_for(STEP_SIZES[COARSE])
check("a slowdown steps down the grid rather than skipping a value",
      abs((top - faller.feed) - step_size) < 1.0
      or faller.feed <= 100 * STEP_SIZES[COARSE] * 60 + 1, True)

# Every step gets the same number of bands, which is what makes the steps feel
# alike to drive: a quarter turn means the same fraction of the step's own top
# speed everywhere. Scaling a grid per millimetre did not do this - 1 mm ended
# up with eight values against 0.5 mm's twelve, because the ceilings are not
# proportional to the step.
for step in STEP_SIZES:
    ceiling = STEP_MAX_FEED[STEP_SIZES.index(step)]
    check("{0} mm has exactly {1} bands".format(step, FEED_BANDS),
          abs(ceiling / quantum_for(step) - FEED_BANDS) < 1e-9, True)

# Without a Bf: figure there is no ground truth, so the old modelled behaviour
# has to survive - a controller with the buffer-state bit off still has to jog.
enc, sched = new_scheduler()
check("no planner report falls back to the modelled queue", sched.planner_capacity, 0)
enc.move(4 * 3)
check("  and still emits", motion(sched.tick()) is not None, True)

# Capacity is learned from the largest free count seen, so it needs no constant
# and follows a controller with a different planner size. Through the setter,
# not by assigning the attribute: learning it anywhere else is what made a
# still pendant look like a controller with no Bf: report at all.
enc, sched = new_scheduler()
sched.set_planner_free(35)
check("capacity is learned from the reported maximum", sched.planner_capacity, 35)

print()
print("attack window")

# The long rate window is a trailing average, so it lags a hand winding on.
# From rest to speed across the width of the window, the average at the end of
# the ramp is about half what is actually being turned - so the step's ceiling
# could not be reached by arriving at the right speed, only by holding it long
# enough for the window to refill.
#
# Reported from the machine as a dead stop being hard to accelerate away from,
# while the same top speed came easily by turning slowly first and then winding
# on. Priming leaves the window full of moving samples, so an increase reads at
# once; the wheel speed was never the difficulty.

SLOW_PER_TICK = 1
FAST_PER_TICK = 6

enc, sched = new_scheduler()
sched.set_step(STEP_SIZES[COARSE])

# Fill the window at a steady slow wind, so nothing here is a start transient.
for _ in range(RATE_WINDOW_TICKS * 2):
    enc.move(SLOW_PER_TICK * COUNTS_PER_DETENT)
    sched.tick()

# Then wind on hard, for less time than the long window is wide.
#
# Long enough for the attack window to turn over, not merely to fill. Ticks are
# computed from overlapping windows, so consecutive wins are not independent
# evidence until the detents being judged are new ones - which is why the hold
# outlasts the window and why this waits for it. Still six ticks against the
# twenty of the trailing average, so the property under test is untouched.
for _ in range(RATE_ATTACK_HOLD_TICKS):
    enc.move(FAST_PER_TICK * COUNTS_PER_DETENT)
    sched.tick()

seen = sched.turn_rate(FAST_PER_TICK)
slow_rate = SLOW_PER_TICK / (TICK_MS / 1000.0)
fast_rate = FAST_PER_TICK / (TICK_MS / 1000.0)

# The long window is still dominated by the slow stretch; the attack window is
# not. Not asked to reach the hand exactly - it is only RATE_ATTACK_TICKS wide -
# only to be far nearer it than the trailing average.
check("a wind-on is seen before the long window catches up",
      seen > slow_rate * 3, True)
# The hold has to outlast the window or it proves nothing, and stay far short
# of the long window or it has replaced the thing it exists to pre-empt.
check("  the attack hold outlasts its own window",
      RATE_ATTACK_HOLD_TICKS > RATE_ATTACK_TICKS, True)
check("  and still beats the trailing average comfortably",
      RATE_ATTACK_HOLD_TICKS * 3 <= RATE_WINDOW_TICKS, True)
check("and is never read as faster than the hand",
      seen <= fast_rate * 1.01, True)

# But an unsteady hand at a constant speed must not trip it, or the short
# window turns ordinary wobble into feed changes - measured as half-speed
# bursts going from 4-5% of blocks changing feed to 13-18%.
enc3, sched3 = new_scheduler()
sched3.set_step(STEP_SIZES[COARSE])
STEADY = 4
for i in range(SETTLE):
    # Alternating 3 and 5 detents a tick: a 25% wobble either side of four,
    # which is well inside what a hand does while holding a speed.
    wobble = 3 if i % 2 else 5
    enc3.move(wobble * COUNTS_PER_DETENT)
    sched3.tick()

steady_rate = STEADY / (TICK_MS / 1000.0)
check("a wobbling hand at a steady speed does not trip the attack window",
      sched3.turn_rate(STEADY) <= steady_rate * RATE_ATTACK_MARGIN, True)

# Steady turning must be unaffected: both windows agree, so the higher of the
# two is simply the rate.
enc2, sched2 = new_scheduler()
sched2.set_step(STEP_SIZES[COARSE])
for _ in range(SETTLE):
    enc2.move(FAST_PER_TICK * COUNTS_PER_DETENT)
    sched2.tick()

check("a steady turn reads the same as before",
      sched2.turn_rate(FAST_PER_TICK), fast_rate, tol=fast_rate * 0.02)


print("measured tick period")

# TICK_MS is the sleep at the end of the loop, not the period of it: run() does
# its work and *then* sleeps, so the real interval is the work plus TICK_MS.
# Measured on the board at about 27 ms against a nominal 20 - the pendant sent
# ~37 messages a second where 50 was intended, in every burst of every session.
#
# Taking the nominal figure as the period made every rate estimate 35% high, so
# the machine was commanded a third faster than the hand was turning, drained
# its own buffer and stopped. That was the jerk to zero.

REAL_TICK_MS = 27
PER_TICK = 4

_clock = [0]
_real_ticks_ms = time.ticks_ms
time.ticks_ms = lambda: _clock[0]

enc4, sched4 = new_scheduler()
sched4.set_step(STEP_SIZES[COARSE])

for _ in range(300):
    _clock[0] += REAL_TICK_MS
    enc4.move(PER_TICK * COUNTS_PER_DETENT)
    sched4.tick()

check("a slow tick is measured rather than assumed",
      abs(sched4._tick_ms - REAL_TICK_MS) < 1.0, True)

true_rate = PER_TICK / (REAL_TICK_MS / 1000.0)
nominal_rate = PER_TICK / (TICK_MS / 1000.0)
seen = sched4.turn_rate(PER_TICK)

check("the rate follows real elapsed time",
      abs(seen - true_rate) < true_rate * 0.05, True)
check("and not the nominal tick, which read 35% high",
      seen < nominal_rate * 0.85, True)

# The detent budget has to move with it. It is derived from the feed times the
# tick, so the two errors used to cancel - a rate 35% high multiplied by a tick
# 35% short came out roughly right. Correcting only one would have started
# discarding a quarter of the operator's wheel motion.
budget_ratio = sched4._tick_ms / TICK_MS
check("the detent budget uses the same measured tick",
      budget_ratio > 1.25, True)

# A stall is real elapsed time but it is not the period; folding one in would
# move the estimate for the rest of the burst.
before = sched4._tick_ms
_clock[0] += 500
enc4.move(PER_TICK * COUNTS_PER_DETENT)
sched4.tick()
check("a loop stall does not move the measurement",
      abs(sched4._tick_ms - before) < 0.01, True)

time.ticks_ms = _real_ticks_ms


if failures:
    print("{} FAILED: {}".format(len(failures), ", ".join(failures)))
    sys.exit(1)
print("all jog scheduler tests passed")
