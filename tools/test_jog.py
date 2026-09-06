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
                         RATE_ATTACK_TICKS, RATE_ATTACK_MARGIN,
                         COUNTS_PER_DETENT,
                         STEP_MAX_FEED, AXIS_MAX_FEED,
                         FEED_DEADBAND,
                         PLANNER_TARGET_BLOCKS, PLANNER_FILL_RATIO,
                         RUNAHEAD_LIMIT_S, MIN_RUNAHEAD_MM,
                         MIN_FEED_BLOCK_MS)

# Index by value, so adding a step to the ladder cannot silently retarget a
# test at a different step size.
COARSE = STEP_SIZES.index(1.0)
FINE = STEP_SIZES.index(0.01)

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
    for _ in range(SETTLE):
        enc.move(4 * rate)
        message = sched.tick()
    per_detent.append(abs(message["det"] * message["step"]) / rate)
check("distance per detent is identical at every turn speed",
      all(abs(d - 0.1) < 1e-9 for d in per_detent), True)

# Feed must never fall below the rate being commanded, or the shortfall queues
# every tick and runs on after the wheel stops.
enc, sched = new_scheduler()
sched.set_step_index(COARSE)             # 1.0 mm per detent
for _ in range(SETTLE):
    enc.move(4)                          # 1 detent per tick
    tracked = sched.tick()
check("feed meets the commanded rate",
      tracked["feed"], feed_for(1, STEP_SIZES[COARSE]),
      tol=near(feed_for(1, STEP_SIZES[COARSE])))

# Feed must not exceed the commanded rate. Over-feeding makes each move finish
# early and stop, so the planner accelerates and decelerates once per detent -
# which is what made fine steps violent on the machine.
enc, sched = new_scheduler()
for _ in range(SETTLE):
    enc.move(4 * 3)
    fine = sched.tick()
check("feed never exceeds the commanded rate",
      fine["feed"], feed_for(3, 0.1), tol=near(feed_for(3, 0.1)))

# Which means a move lasts about a full tick, so consecutive jogs join up
# instead of each one starting and stopping.
move_ms = (abs(fine["det"]) * fine["step"] / (fine["feed"] / 60.0)) * 1000
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
for _ in range(SETTLE):
    enc.move(4 * 6)
    spinning = sched.tick()
check("fast turning raises the feed",
      spinning["feed"], min(feed_for(6, 0.1), STEP_MAX_FEED[2]),
      tol=near(min(feed_for(6, 0.1), STEP_MAX_FEED[2])))

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
    for _ in range(step_index):
        sched.step_up()
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
starved, sent_starved = run_at_depth(CAPACITY)
supplied, sent_supplied = run_at_depth(CAPACITY - PLANNER_TARGET_BLOCKS)
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
starved_far_behind, sent_far = run_at_depth(CAPACITY, lag_mm=10000.0)
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
for _ in range(RATE_ATTACK_TICKS):
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


if failures:
    print("{} FAILED: {}".format(len(failures), ", ".join(failures)))
    sys.exit(1)
print("all jog scheduler tests passed")
