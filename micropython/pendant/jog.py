"""Turns handwheel motion into jog messages.

Sits between the encoder and the link. Accumulated detents are coalesced and
emitted on a fixed tick rather than sent per click, because a 100 PPR wheel
spun hard produces ~500 clicks/s and one message each would flood the link for
no gain - the sender cannot act on them faster than it dispatches anyway.

Tick rate is set below the sender's dispatch interval, so the sender forwards
each message as it arrives rather than adding a second, unsynchronised
quantiser on top. Above it, messages merge into longer blocks - and block
count, not block length, is what lets the controller's planner hold a feed.

Jog cancel is sent once when motion stops, and again whenever the axis changes
mid-motion, so a partially executed jog on the old axis does not continue after
the operator has moved on.
"""

try:
    import protocol
except ImportError:
    from pendant import protocol


# 20 ms.
#
# This has been 20, 50, 100 and back, and most of those moves were made against
# a theory that turned out to be wrong: that grblHAL was being flooded with jog
# blocks. It was not. The stalls were the sender posting every jog to its UI
# thread, where they queued behind rendering and console trimming, and the fix
# belonged there.
#
# With that gone the constraint is the opposite of what was assumed. What the
# planner needs to hold a commanded feed is a *number* of blocks, not a
# distance: at F9000 a 7.5 mm block reaches only sqrt(1500 x 7.5) = 106 mm/s on
# its own, so at least two must chain to make 150. A 50 ms tick held one to
# four, right on that threshold, and the reported feed then walked 5400, 6300,
# 7200, 8100, 9000 - exactly sqrt(1500 x d) for the distance chained at each
# moment. Continuous re-deciding, felt as steady roughness.
#
# A shorter tick buys lookahead without buying run-off, which is the whole
# point. Run-off is the distance queued; chaining depends on the count. At
# 20 ms the same 20 mm of lead is seven blocks rather than three.
#
# The sender's dispatch interval has to come down with it, or the short blocks
# are merged straight back into long ones.
TICK_MS = 20

# Quadrature edges per detent, matching the decoder.
COUNTS_PER_DETENT = 4

# Millimetres per detent.
#
# The right traverse step is the one where a natural turning speed lands on the
# machine's maximum feed: step = feed_max / (60 x detents_per_second). At around
# 300 detents/s that is roughly 0.28 mm, which is why 1 mm felt dead - it asks
# for ~18000 mm/min, the machine delivers 5000, and the rest is dropped. 0.5 mm
# fills that gap.
#
# 5 mm is suspended for the same reason taken further: it reaches 5000 mm/min at
# 17 detents/s, barely turning, and everything above pins at maximum with no
# proportional feel left.
STEP_SIZES = (0.001, 0.01, 0.1, 0.5, 1.0)
DEFAULT_STEP_INDEX = 2

# Silence that counts as "the operator stopped" rather than "turning slowly".
#
# Cancel flushes queued motion, which is the point - stopping the wheel should
# halt the machine rather than let it run on through a backlog. The risk is
# firing during a slow turn and truncating a jog mid-move. Someone winding
# continuously produces a detent at least every ~300 ms even when crawling, so
# anything quieter than that is a genuine stop.
#
# Raised to 2.5 s after machine testing, in two stages.
#
# At 300 ms it cancelled between individual detents during deliberate slow
# turning - jog, cancel, jog, cancel - which reads as the machine refusing to
# move. At 600 ms it still fired whenever the operator re-gripped the wheel,
# which a long traverse forces repeatedly: 49 inches is 12 revolutions at 1 mm
# and 25 at 0.5 mm, and each pause produced a dead stop, a pause, then a ramp
# back up. Three of them per traverse, twice as often at the finer step -
# exactly the ratio of revolutions required.
#
# What makes a long threshold safe is that the cancel is now nearly redundant.
# In-flight motion is bounded to a few ticks, so stopping the wheel stops the
# machine within about 100 ms on its own. The cancel mattered when the queue
# could grow without limit; now it is a safety net rather than the mechanism.
IDLE_MS_BEFORE_CANCEL = 2500
IDLE_TICKS_BEFORE_CANCEL = max(1, IDLE_MS_BEFORE_CANCEL // TICK_MS)

# --- planner depth regulation ---------------------------------------------
#
# The controller reports free planner slots in Bf:. That is ground truth for
# how much work it actually holds, and it replaced a model that was wrong.
#
# The model said the pendant had tens of millimetres queued ahead. The machine
# said it held two to four blocks of a 128-slot planner, and intermittently
# nothing at all - free at the maximum, reported feed at zero, for around
# 150 ms at a time. That is a dead stop mid-traverse, and it is the jerk.
#
# Six blocks is 300 ms at a 50 ms tick, against a longest observed stall of
# 150 ms.
#
# Four was tried, to cut run-off. It did - but it also made 0.1 mm noticeably
# rougher, because that is where the planner is already starved: the feed
# matches the hand, so there is no surplus distance to bank and depth sits at
# one or two blocks whatever the target. Lowering the target there removes the
# only pressure filling it at all.
#
# Serving both from this one number is what forced the trade. Depth costs
# run-off in proportion to block length, so the same six blocks is 12 mm at
# 0.1 mm and 45 mm at 0.5 mm - fine at one step and too much at the other.
# RUNAHEAD_LIMIT_S bounds the run-off directly instead, which frees this to be
# set for smoothness alone.
#
# It happened because emission was capped at exactly what the commanded feed
# drains in a tick. Sending precisely what the machine consumes means depth can
# never build: the pipeline runs two to four blocks deep forever, so any hiccup
# anywhere along pendant -> wifi -> sender -> usb -> controller empties it. The
# sender's own console showed the hiccups directly, as merged blocks - X30 and
# X37.5 where 7.5 was expected, five messages' worth dispatched at once after a
# stall.
#
# So run ahead of the drain until the controller holds a real cushion, then
# match the drain to hold it there. Depth is measured, not assumed.
PLANNER_TARGET_BLOCKS = 6

# Ceiling on how far above the drain rate to emit, reached only when the
# planner is completely empty. Two means the buffer gains a tick of work per
# tick at worst, which fills it before the operator is up to speed.
#
# Applied proportionally to the shortfall, not as a switch. Switching between
# this and the drain rate on a threshold is bang-bang control: depth chatters
# across the target and block length doubles and halves tick to tick. The
# machine showed it as alternating 6.5 mm and 13.5 mm blocks at a constant
# F8362, and at 0.1 mm as anything from 1.2 mm to 3.4 mm. Blocks of unequal
# length take unequal time at a fixed feed, so a short one landing on a
# shallow planner is a stumble - which is precisely the roughness that
# remained after depth regulation fixed the outright stalls.
#
# Scaling by the shortfall makes the correction fade out as the buffer fills,
# so block length converges instead of oscillating.
PLANNER_FILL_RATIO = 2.0

# Hard bound on how far behind the hand the machine may run, in seconds.
#
# Depth alone is not a sufficient bound. At 0.5 mm the planner never reaches
# target - the feed matches the hand, so distance sent per tick equals distance
# drained per tick and there is no surplus to bank - and the fill correction
# then runs continuously. The excess does not become depth; it becomes lag. The
# machine measured 67 mm behind steady and 96 mm at peak, which is the run-off
# felt on stopping.
#
# Expressed in time rather than millimetres because that is what it costs to
# stop: run-off is the buffer emptying at the commanded feed, so the same
# fraction of a second is the same feel at any speed. Below this bound the
# depth regulator does as it likes; above it, emission drops to the drain rate
# no matter how shallow the planner is.
#
# Raised from a quarter second once the tick came down to 20 ms. At a quarter
# second this was the active limiter rather than a safety net: it allowed 36 mm
# at F8625 while the measured lag sat at 36 to 79, so emission was pinned at
# the drain and the planner held three to four blocks. At 3 mm a block that is
# 9 to 12 mm of chained distance, and holding 150 mm/s needs v^2/a = 15 mm - so
# the machine could never quite reach the commanded feed.
#
# What makes a looser bound safe is that the depth target is now reachable.
# Six blocks is 18 mm at a 20 ms tick where it was 45 mm at 50 ms, so the fill
# completes, emission falls back to the drain, and lag stops growing on its
# own. This is left as a guard for the case where it does not.
#
# Note the measured lag is not all backlog: a status report arriving 100 ms
# late is 15 mm of apparent lag at 150 mm/s with nothing queued behind it. A
# bound set close to the real figure therefore binds well before the buffer is
# actually that deep, which is the mistake this had already made once at a fine
# step.
RUNAHEAD_LIMIT_S = 0.5

# Floor under that bound, in millimetres.
#
# The measured lag is not all deliberate. Wifi, the sender's buffer, the
# controller's receive buffer and the delay before a position is reported back
# all contribute, and at a fine step they dominate: 0.1 mm measured 13-17 mm of
# lag with the planner holding one block. A quarter second at F2375 is only
# 9.9 mm, so the bound was already exceeded before any filling happened and it
# suppressed the fill permanently - the planner sat empty, the reported feed
# collapsed repeatedly, and a fifth of the operator's detents were discarded
# because emission was pinned at the drain rate.
#
# The floor keeps the bound clear of that baseline at low feed, where it was
# never the problem. The time term still governs at speed, which is where
# run-ahead actually runs away.
MIN_RUNAHEAD_MM = 25.0

# --- turn rate drives feed, not distance ----------------------------------
#
# Spinning faster raises the feed rate. It does not multiply the distance.
#
# Scaling distance is the obvious approach and it is wrong. Commanding several
# times more travel than the machine can execute while the wheel is turning
# builds a queue, and then how far the axis actually moved depends on how deep
# that queue was when you stopped - it either runs on, or a cancel discards an
# unpredictable part of it. Either way the wheel stops meaning anything
# specific.
#
# Holding distance at exactly one step per detent and raising the feed instead
# keeps the queue shallow: each small move completes quickly, so stopping the
# wheel stops the machine almost immediately. That is what a hardwired pendant
# feels like.
#
# The natural target for the feed is the rate the operator is already
# commanding. Winding 50 detents/s at 1 mm per detent asks for 50 mm/s, so
# feeding at exactly that makes the machine track the hand.
FEED_TRACKING_ENABLED = True

# Floor and ceiling. The floor is deliberately low: it exists only so a feed of
# zero is never commanded, and raising it re-creates the over-feeding that makes
# short moves violent. The ceiling should sit at or below the machine's own
# maximum jog rate, since asking for more than it can deliver rebuilds the queue
# this design exists to keep shallow.
FEED_MIN_MM_MIN = 50.0

# Longest a single-detent block may take to execute, in milliseconds.
#
# An absolute feed floor cannot serve every step. 50 mm/min was chosen when the
# step was 0.1 mm, where one detent takes 120 ms. At 0.5 mm the same floor takes
# 600 ms - thirty ticks - and the machine crawls through it while everything
# sent behind it queues up. The machine showed exactly that: a reported feed of
# 50 against a commanded 8550, free falling to 111 as seventeen blocks piled in
# behind, and the commanded position running 135 mm ahead of the axis.
#
# So bound the block instead of the feed. A lone deliberate click still moves
# precisely, it just does not hold the pipeline open while it does.
MIN_FEED_BLOCK_MS = 100.0
FEED_MAX_MM_MIN = 15000.0

# Per-axis maximum, from the machine's own settings ($110/$111/$112). Z is
# usually much slower than X and Y, and commanding a feed an axis cannot reach
# only runs it at full acceleration, so the ceiling has to follow the axis
# rather than be one number for the machine.
AXIS_MAX_FEED = {"X": 15000.0, "Y": 15000.0, "Z": 6000.0, "A": 6000.0}

# Smoothing applied to the feed, as an exponential moving average.
#
# Interval measurement reacts within a single detent, which is what fixed the
# wind-down stutter, but hand turning is irregular detent to detent so the raw
# figure jumps around and the motion feels rough. This damps a one-tick spike
# while still following a real change within a few ticks - roughly 50 ms at 0.4.
# Fractional change in the target before the commanded feed follows it.
#
# grblHAL blends consecutive moves that share a feed rate, so a feed drifting
# every message forces a velocity change between every block and is felt as
# jerking. A deadband produces long runs of identical F instead.
#
# Outside the band the feed jumps straight to the target rather than easing
# towards it. Easing plus a deadband is what stranded the feed short: each step
# shrinks until the remaining gap falls inside the band, and there it stops -
# permanently below the rate being commanded. Under-feeding is the harmful
# direction, because distance then arrives faster than it drains, the queue
# grows and the bound starts discarding, which is the jerk returning.
#
# Ten percent, symmetric. The rate average already removes jitter, so this only
# has to absorb what survives it.
FEED_DEADBAND = 0.10

# Ceiling per step size, matching STEP_SIZES.
#
# The fine values are measured on the machine rather than derived - they came
# out roughly a third of what seemed reasonable on paper, which says a fine step
# wants control far more than speed.
#
# The coarse ones now run to the machine's real limit, because anything less
# throws away turning the operator is actually doing. Measured on the machine at
# roughly 4 rev/s - 400 detents/s - the demand is step x 400 x 60:
#
#   0.5 mm -> 12000 mm/min, which X and Y can deliver in full
#   1.0 mm -> 24000 mm/min, which nothing can, so 1 mm sheds ~38% at that speed
#
# So 0.5 mm is the traverse step for a hand that turns this fast, and 1 mm is
# only fully usable below about 250 detents/s. That is a property of the wheel
# and the machine, not something a ceiling can fix.
STEP_MAX_FEED = (150.0, 250.0, 2500.0, 9000.0, 12000.0)

# Rate is measured over a window rather than per tick: at 20 ms a tick sees one
# or two detents even during a fast spin, far too coarse to estimate speed from.
#
# Measured in raw quadrature counts rather than whole detents, which is four
# times the resolution for nothing. Counting detents gave a granularity of one
# detent per window - 5 detents/s, or a 300 mm/min jump at the 1 mm step - so
# the feed staircased instead of ramping. Counts plus a slightly longer window
# bring that to roughly 50 mm/min, which reads as smooth.
#
# Eight ticks, 160 ms. Long enough to average out the whole-detent quantisation
# of a single tick, short enough that winding down lowers the feed promptly -
# the fault a 300 ms window produced.
# Held at about 400 ms of history. Expressed in ticks but sized in time: a
# fixed count silently shortens the averaging window when the tick rate rises,
# and a shorter window means a noisier rate estimate feeding straight into the
# commanded feed.
RATE_WINDOW_TICKS = max(4, 400 // TICK_MS)

# Per-tick trace, for catching a stall in the act.
# Ticks without a detent before the wheel counts as no longer driving. Short,
# because its only job is to tell a wind-down from a stall: two ticks of
# silence at a 20 ms tick is 40 ms, well inside any real turn.
MOVING_GRACE_TICKS = 3

TRACE_TICKS = 24            # how much history to keep either side
                            # (ticks, not time - this is a display width)
# Minimum gap between dumps, so one stall produces one dump rather than a
# screenful. In ticks, but sized in time - at 100 it was five seconds at a
# 50 ms tick and became two when the tick shortened.
# Full table dumps allowed per session, or 0 to print nothing at all.
#
# Set this to 0 for machine work. Everything is still counted and still
# reported in the periodic one-liner - collapses, stalls, dropped detents - so
# nothing is lost except the tables, and the tables are only useful while
# someone is reading them.
#
# Worth knowing why it matters only while tethered: with no USB host attached
# MicroPython discards console output, so a pendant running standalone from
# main.py cannot stall on printing however much it writes. Attached to
# mpremote it can, which makes this a fault that only occurs while being
# watched.
#
# After the budget the reason still prints as one line and the counters keep
# counting, but the twenty-four rows stop.
#
# Printing is not free on this board: every line goes out over USB CDC, and if
# the host is not draining as fast as the pendant writes, print() blocks - and
# blocks the whole asyncio loop with it, including the socket read and the jog
# scheduler. A run with seventy collapses stalled the loop for a full second,
# which is the diagnostic causing the fault it exists to describe.
#
# Six is enough to see a pattern. The sixtieth dump has never said anything the
# sixth did not.
TRACE_DUMP_BUDGET = 6

TRACE_QUIET_MS = 5000
TRACE_QUIET_TICKS = max(1, TRACE_QUIET_MS // TICK_MS)
TRACE_STUMBLE_RATIO = 0.4   # a tick carrying less than this share of the recent
                            # average, while still turning, is a stumble


class JogScheduler:
    """Coalesces encoder detents into paced jog messages.

    `tick()` carries all the logic and returns the message to send, or None.
    Keeping it a plain method with no I/O of its own means the behaviour can be
    tested against a fake encoder on a PC, without hardware or a network.
    """

    def __init__(self, encoder, axis="X", step_index=DEFAULT_STEP_INDEX):
        self.encoder = encoder
        self.axis = axis
        self.step_index = step_index
        self.enabled = True

        self.feed_tracking = FEED_TRACKING_ENABLED

        self._residual = 0
        self._idle_ticks = 0
        self._moving = False
        self._recent = []          # raw counts per tick, most recent last
        self.feed = FEED_MIN_MM_MIN   # last applied, for display
        self._queue_mm = 0.0          # estimate of motion in flight
        self._ticks_since_motion = 0  # interval used to measure turn rate
        self._direction = 0           # sign of motion currently in flight
        self._settled = FEED_MIN_MM_MIN   # deadbanded feed, before any trim
        self.actual_feed = 0          # what the controller reports, pushed in
        self.planner_free = 0         # controller's free planner slots, ditto
        self.planner_capacity = 0     # largest free count seen = empty planner
        self.dumps = 0                # full tables printed, against the budget
        self.lag_mm = 0.0             # measured, pushed in from the status feed

        # Rolling per-tick history, dumped when a stumble is detected. A 15 s
        # summary cannot show what happens in the 300 ms around a stall, and
        # three separate explanations for these stalls have now failed to
        # reproduce in simulation - so record the real thing instead of
        # reasoning about a model of it.
        self.trace = []
        self.stumbles = 0
        self._last_dump = -TRACE_QUIET_TICKS
        self.stats = {"messages": 0, "detents": 0, "cancels": 0,
                      "dropped_detents": 0, "reversals": 0}

        # Signed distance commanded, for comparison against what the
        # machine reports actually moving. The scheduler's own queue is
        # an open-loop model and cannot see the controller's planner or
        # the sender's buffer, so it cannot detect a backlog building
        # there. This can.
        self.commanded_mm = 0.0

    # --- configuration ----------------------------------------------------

    @property
    def step(self):
        return STEP_SIZES[self.step_index]

    def set_step_index(self, index):
        self.step_index = max(0, min(index, len(STEP_SIZES) - 1))
        return self.step

    def set_step(self, step):
        """Select a step by value rather than by position on the ladder.

        The touch grid knows values, not indices - it draws them - and
        resolving here keeps the layout from having to know the ladder's order.
        A value not on the ladder is ignored rather than clamped: it means the
        grid and STEP_SIZES have drifted, and silently selecting the nearest
        step would hide that.
        """
        for index, value in enumerate(STEP_SIZES):
            if value == step:
                return self.set_step_index(index)
        return self.step

    def step_up(self):
        return self.set_step_index(self.step_index + 1)

    def step_down(self):
        return self.set_step_index(self.step_index - 1)

    def set_axis(self, axis):
        """Change axis. Returns a cancel message if a jog was in progress.

        Motion already commanded on the previous axis has to be stopped
        explicitly; otherwise it keeps running while the operator believes they
        have switched to a different one.
        """
        if axis == self.axis:
            return None
        self.axis = axis
        self._residual = 0
        self._queue_mm = 0.0
        self.encoder.take()  # discard motion that arrived during the switch
        if self._moving:
            self._moving = False
            self._idle_ticks = 0
            self.stats["cancels"] += 1
            return protocol.jog_cancel()
        return None

    # --- per tick ---------------------------------------------------------

    def turn_rate(self, detents=0):
        """Detents per second.

        Averaged over a short window while turning, and taken from the interval
        since the last movement when starting from rest.

        The window exists because a tick sees a small whole number of detents -
        2, 3 or 4 at a normal wind - so a per-tick reading jitters by a third
        even from a perfectly steady hand. That jitter passes straight through
        the feed and into the planner. Averaging removes it; the window is kept
        short so winding down still lowers the feed promptly, which is what a
        long window got wrong.

        Starting from rest there is no history to average, so the interval since
        the last movement is used instead - accurate from the very first detent,
        which is what keeps the opening of a turn from being throttled.

        That interval only means anything for a single detent, though. Several
        arriving inside one tick demonstrably arrived within that tick, whatever
        preceded them, so the tick is the measurement window. Dividing them by
        the whole idle gap instead reported a near-zero turn rate for the first
        detent after any pause, so a burst opened at the floor feed and took
        nine ticks to reach speed - felt as having to spin the wheel a while
        before the machine responds.
        """
        if not detents:
            return 0.0

        if not self._moving:
            if abs(detents) > 1:
                return abs(detents) / (TICK_MS / 1000.0)
            # A lone detent says only that one arrived somewhere in the gap, so
            # the gap is the best estimate - and a deliberate single click for
            # fine positioning stays slow, which is the point.
            ticks = self._ticks_since_motion
            if ticks < 1:
                ticks = 1
            return abs(detents) / (ticks * TICK_MS / 1000.0)

        total = 0
        for value in self._recent:
            total += abs(value)
        # Averaged over the samples actually held, not the nominal window
        # length. Dividing by the full length while it is still filling reports
        # a rate lower than the hand is really turning, so the feed dips just
        # after a strong start.
        samples = len(self._recent) or 1
        seconds = samples * TICK_MS / 1000.0
        return (total / COUNTS_PER_DETENT) / seconds

    def feed_floor(self):
        """Lowest feed worth commanding at the current step.

        Scaled so one detent completes within MIN_FEED_BLOCK_MS rather than
        being a fixed rate, because a fixed rate means a fixed *time* only at
        one step size.
        """
        floor = self.step * 60000.0 / MIN_FEED_BLOCK_MS
        return floor if floor > FEED_MIN_MM_MIN else FEED_MIN_MM_MIN

    def feed_rate(self, detents):
        """Feed in mm/min that matches the current winding speed.

        detents/s x mm/detent x 60 is exactly the rate the operator is asking
        for, so the machine tracks the hand rather than lagging behind it or
        racing ahead. Clamped at both ends: a floor so a careful turn is not
        glacial, and a ceiling because commanding more than the machine can
        deliver only rebuilds the queue.
        """
        if not self.feed_tracking:
            return FEED_MAX_MM_MIN

        # Exactly the rate being commanded - no more, except briefly at the
        # start of a burst while the planner's buffer is established.
        #
        # A trim below the arrival rate while a buffer filled was tried and
        # removed. It could not tell a filling buffer from a full one, because
        # the queue is measured after the tick's drain and so always reads at its
        # trough - so the trim never released and the feed sat permanently under
        # the arrival rate, which is the direction that grows a backlog. The
        # smoothest run observed on the machine was one where a ceiling was
        # binding, and a bound feed is a constant feed regardless of any of
        # this.
        #
        # Feeding faster than commanded is not free, which an earlier version
        # got wrong. Distance per detent is fixed, so a higher feed does not
        # cover more ground; it makes each move finish early and then wait. At
        # 0.1 mm and 5000 mm/min a move lasts 1.2 ms of a 20 ms tick, so the
        # planner accelerates hard and decelerates to a stop for every detent -
        # felt on the machine as rough, violent motion at fine steps while the
        # coarse step, where feed happened to be near the commanded rate, was
        # smooth.
        #
        # Matching the commanded rate makes each move exactly fill its tick, so
        # consecutive jogs blend into continuous motion. terjeio's MPG firmware
        # does the same thing - feed straight from encoder velocity, with no
        # curve above it.
        target = self.turn_rate(detents) * self.step * 60.0

        ceiling = STEP_MAX_FEED[self.step_index]
        axis_ceiling = AXIS_MAX_FEED.get(self.axis, FEED_MAX_MM_MIN)
        if axis_ceiling < ceiling:
            ceiling = axis_ceiling
        if target > ceiling:
            target = ceiling
        else:
            floor = self.feed_floor()
            if target < floor:
                target = floor

        # Smooth towards the target rather than jumping to it - but only while
        # already moving. Starting from rest, smoothing would ramp up from the
        # floor over several ticks, and since the queue bound derives from the
        # feed it would also under-estimate capacity and drop detents at exactly
        # the moment the operator starts turning. Smoothing exists to damp
        # jitter during a turn, not to soften its start.
        if not self._moving:
            return target

        # Hold unless the target has left the band; otherwise take it exactly.
        #
        # Compared against the untrimmed settled value, never against the
        # commanded feed. The commanded feed already has the trim in it, so
        # comparing to that re-trims an already-trimmed number every tick and
        # the gap alternately clears and fails the band - a perfect two-cycle
        # that put a different feed on every message and stopped grblHAL
        # blending any of them.
        if abs(target - self._settled) < self._settled * FEED_DEADBAND:
            settled = self._settled
        else:
            settled = target
        self._settled = settled

        # A queue-model trim used to sit here, shading the feed 5% below the
        # arrival rate while _queue_mm said the buffer was filling, so the
        # machine drained slower than the pendant sent and depth accumulated.
        #
        # It is gone. Planner depth is measured now rather than modelled, and
        # regulated by how much is emitted rather than by the feed - so the
        # trim was a second regulator pulling on the same quantity, and the one
        # steering on a figure the controller kept contradicting. The model
        # read 220 mm on the run where the machine reported 36.
        #
        # _queue_mm survives as a trace column and as the input to the
        # starvation detector, where being approximate costs nothing.
        floor = self.feed_floor()
        if settled < floor:
            settled = floor
        return settled

    def tick(self):
        """Advance one interval. Returns a message to send, or None."""
        counts = self.encoder.take()

        # Drain the in-flight estimate by what the machine executes in a tick at
        # the feed last commanded. Done every tick, including idle ones, so the
        # queue empties while the wheel is still.
        drained = (self.feed / 60.0) * (TICK_MS / 1000.0)
        self._queue_mm -= drained
        if self._queue_mm < 0:
            self._queue_mm = 0.0

        if not self.enabled:
            # Keep draining the encoder so motion while disabled is discarded
            # rather than surfacing all at once when it is re-enabled.
            self._residual = 0
            return None

        self._residual += counts

        # Whole detents only. The remainder stays put for the next tick so slow
        # turning accumulates instead of being repeatedly rounded away.
        detents = int(self._residual / 4)
        self._residual -= detents * 4

        self._ticks_since_motion += 1

        # Rate history advances every tick, including empty ones - otherwise a
        # pause would keep the previous speed alive and the next slow detent
        # would arrive at the old feed.
        #
        # Raw counts, not detents: the residual that has not yet formed a whole
        # detent still represents wheel movement, and including it is what gives
        # the finer resolution.
        self._recent.append(counts)
        if len(self._recent) > RATE_WINDOW_TICKS:
            self._recent.pop(0)

        if detents:
            # A burst starts with a clean window. Idle ticks are appended above
            # so a pause mid-turn decays the feed, which is right - but carrying
            # them into a fresh start divides the first real samples by the
            # whole window and reports a fraction of the true turn rate. That
            # was the ramp: nine ticks climbing from the floor before the
            # machine matched the hand.
            if not self._moving:
                self._recent = [counts]

            # A reversal has to flush what is queued. The buffer that keeps the
            # planner supplied is motion in the old direction, so without this
            # the machine travels the whole of it the wrong way before it starts
            # coming back - and the deeper the buffer, the worse the reversal.
            # It is why buffering helped a steady wind and not a back-and-forth.
            direction = 1 if detents > 0 else -1
            if self._direction and direction != self._direction and self._queue_mm > 0:
                self._direction = 0
                self._queue_mm = 0.0
                self._residual = 0
                self.stats["reversals"] += 1
                return protocol.jog_cancel()
            self._direction = direction

            self._idle_ticks = 0

            # Feed is computed before _moving is set, because feed_rate reads
            # it to tell a fresh start from a continuing turn. Setting it first
            # made every start look like a continuation, so the feed smoothed up
            # from the floor - and since the queue bound derives from the feed,
            # that also discarded most of the opening detents.
            self.feed = self.feed_rate(detents)
            self._moving = True

            # Cap what goes out at what the machine drains in a tick, which is
            # steady while the feed is steady.
            #
            # Capping against remaining queue room instead - the obvious
            # reading of "do not exceed the bound" - makes the cap swing every
            # tick, because room does. Each message then carries a different
            # distance despite a constant feed, so every move takes a different
            # time to execute and the planner keeps running short. That is felt
            # as roughness, and it scales with how much is being discarded:
            # barely visible at 0.1 mm where 10% is dropped, constant at 1.0 mm
            # where half is.
            #
            # Emitting exactly the drain also leaves the queue where it is, so
            # the buffer built at the start of the burst stays put.
            # While building, emit at the untrimmed rate so the queue gains the
            # difference. Once at depth, emit exactly what the commanded feed
            # drains, which holds it there.
            #
            # A second depth check here was tried and reverted: evaluated every
            # tick it toggles, which toggles the cap, which moves the queue, and
            # the feed alternates on every message again - 29 changes in 30. The
            # queue settling around half again over target is the cost of a feed
            # that holds still, and that is the right way round.
            # Regulate on what the controller reports holding, not on the
            # modelled queue. The model tracked intent - detents that arrived -
            # and stayed high while the planner underneath it ran dry, which is
            # why every conclusion drawn from it was wrong.
            #
            # Capacity is the largest free count ever seen, which is the planner
            # empty. Until a Bf: figure arrives capacity is zero and this falls
            # back to the old modelled behaviour, so a controller with the
            # buffer-state bit off still jogs.
            if self.planner_free > self.planner_capacity:
                self.planner_capacity = self.planner_free
            # Run-ahead is bounded before depth is even consulted. Filling is
            # what puts the machine behind the hand, so once it is far enough
            # behind there is nothing a shallow planner can justify.
            runahead_mm = (self.feed / 60.0) * RUNAHEAD_LIMIT_S
            if runahead_mm < MIN_RUNAHEAD_MM:
                runahead_mm = MIN_RUNAHEAD_MM
            if self.planner_capacity and self.lag_mm <= runahead_mm:
                held = self.planner_capacity - self.planner_free
                shortfall = PLANNER_TARGET_BLOCKS - held
                if shortfall <= 0:
                    cap = self.feed
                else:
                    if shortfall > PLANNER_TARGET_BLOCKS:
                        shortfall = PLANNER_TARGET_BLOCKS
                    cap = self.feed * (1.0 + (PLANNER_FILL_RATIO - 1.0)
                                       * shortfall / PLANNER_TARGET_BLOCKS)
            elif self.planner_capacity:
                cap = self.feed
            else:
                # No Bf: report, so there is no depth to regulate against.
                # Emit exactly the drain and say so once, rather than
                # inferring depth from a model - inferring it is what sent
                # this whole effort chasing the wrong layer for days.
                cap = self.feed
            # Rounded, not truncated. Truncating biases every tick downwards,
            # and the bias is what the planner feels: emitting less than the
            # drain is exactly how depth is lost. It also lands on values that
            # should be exact - 900/60 x 0.020 / 0.1 evaluates to
            # 2.9999999999999996, so asking for three detents allowed two.
            #
            # Harmless at a 50 ms tick where a detent is a few percent of the
            # message; at 20 ms the same detent is a third of it.
            allowed = int((cap / 60.0) * (TICK_MS / 1000.0) / self.step + 0.5)
            if allowed < 1:
                allowed = 1

            # No bound-based backstop here. One was tried and removed: the bound
            # follows the feed, so a downward feed step shrank it below a queue
            # that had been fine a tick earlier, collapsed emission to a single
            # detent, and starved the planner. On the machine that is smooth
            # while pinned at a ceiling, then a stumble and a brief stop as the
            # feed comes off it. Emission already equals the drain, so the queue
            # cannot run away and nothing needs catching.
            if abs(detents) > allowed:
                self.stats["dropped_detents"] += abs(detents) - allowed
                detents = allowed if detents > 0 else -allowed
                self._residual = 0

            self._queue_mm += abs(detents) * self.step
            self.commanded_mm += detents * self.step

            self._ticks_since_motion = 0
            self.stats["messages"] += 1
            self.stats["detents"] += abs(detents)
            self._record(detents)
            return protocol.jog(self.axis, detents, self.step, self.feed)

        self._record(detents)

        floor = self.feed_floor()
        if self._moving or self.feed > floor:
            # Let the smoothed feed fall while the wheel is still, or the first
            # detent after a pause would inherit the speed of the last burst.
            self.feed += FEED_DEADBAND * (floor - self.feed)
            if self.feed < floor:
                self.feed = floor

        if self._moving:
            # Stopping the wheel always flushes what is queued. The alternative
            # - letting commanded motion finish on its own - was a mode here
            # while the two were being compared on the machine, and lost: how
            # far the axis ended up depended on how deep the buffer happened to
            # be when the hand stopped, which is exactly the unpredictability
            # this design exists to avoid.
            self._idle_ticks += 1
            if self._idle_ticks >= IDLE_TICKS_BEFORE_CANCEL:
                self._moving = False
                self._idle_ticks = 0
                self._direction = 0
                self.stats["cancels"] += 1
                return protocol.jog_cancel()

        return None

    def _record(self, detents):
        """Keep a rolling trace, and flag the planner running dry.

        Called on every tick, including those that emit nothing. Recording only
        the ticks that emit made a pause invisible while it still drained the
        queue, so a stall and a deliberate stop looked identical - one trace row
        showing a drop of several ticks' drain with nothing to say why.
        """
        carried = abs(detents) * self.step
        # The controller's own feed goes in the same row as the commanded one,
        # so a dump shows whether an actual dip follows a commanded change or
        # happens while the command is perfectly steady. Those are different
        # faults and nothing else distinguishes them.
        # planner_free is the decisive column. The queue column is what this
        # scheduler *believes* it has sent ahead; planner_free is what the
        # controller actually holds. When the two disagree the model is wrong,
        # and every conclusion drawn from the queue column is worthless.
        self.trace.append((round(self.feed), round(self.actual_feed),
                           abs(detents), carried, round(self._queue_mm, 1),
                           self.planner_free))
        if len(self.trace) > TRACE_TICKS:
            self.trace.pop(0)

        if len(self.trace) < TRACE_TICKS:
            return
        # The planner stalls when the queue runs dry, not when a single tick
        # carries less than usual - a dip the buffer absorbs is a non-event, and
        # flagging those would bury the ones that matter.
        drain = (self.feed / 60.0) * (TICK_MS / 1000.0)
        if self._queue_mm > drain:
            return

        recent = [row[3] for row in self.trace[:-1]]
        average = sum(recent) / len(recent)
        if average <= 0:
            return

        # Only a dry queue while the operator is still turning is a fault. One
        # that empties because the wheel is slowing or stopped is the machine
        # doing as it was told, and flagging those buries the real ones.
        typical = sum(row[2] for row in self.trace[:-1]) / (len(self.trace) - 1)
        if abs(detents) < typical * 0.5:
            return

        self.stats["messages"] += 0          # no-op, keeps the counter honest
        self.stumbles += 1
        if self.stats["messages"] - self._last_dump < TRACE_QUIET_TICKS:
            return
        self._last_dump = self.stats["messages"]
        self.dump_trace("starved {}: queue {:.2f} mm".format(
            self.stumbles, self._queue_mm))

    @property
    def moving(self):
        """True while the wheel is turning or its motion is still in flight.

        Read by the collapse detector, which otherwise flags every normal stop:
        the commanded feed decays over a second or so after the last detent
        while the machine finishes what is queued, so a reported feed of zero
        at the end of that is the axis arriving, not a fault.

        Deliberately not _moving, which stays set until the idle timeout at
        2.5 s - far longer than the wind-down - so gating on it flagged the
        stops anyway. What matters here is whether detents are still arriving.
        """
        return self._ticks_since_motion < MOVING_GRACE_TICKS

    def dump_trace(self, reason):
        """Print the rolling trace with a reason. Rate-limited by the caller.

        Degrades to a single line once the budget is spent, rather than
        stopping: knowing an event still happens matters, and the table is what
        costs the time.
        """
        if not TRACE_DUMP_BUDGET:
            return
        self.dumps += 1
        if self.dumps > TRACE_DUMP_BUDGET:
            print("[trace] {} (table suppressed, {} dumps)".format(
                reason, self.dumps))
            return

        print("[trace] {}".format(reason))
        print("  cmdF  actF  det   mm  queue  free")
        for feed, actual, det, mm, queue, free in self.trace:
            print("  {:>5} {:>5} {:>4} {:>5.2f} {:>5.1f} {:>5}".format(
                feed, actual, det, mm, queue, free))
        if self.dumps == TRACE_DUMP_BUDGET:
            print("  (dump budget spent - further traces print one line)")

    async def run(self, link):
        """Drive the scheduler forever, handing messages to the link."""
        import asyncio

        while True:
            message = self.tick()
            if message is not None:
                link.send(message)
            await asyncio.sleep_ms(TICK_MS)
