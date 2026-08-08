"""Turns handwheel motion into jog messages.

Sits between the encoder and the link. Accumulated detents are coalesced and
emitted on a fixed tick rather than sent per click, because a 100 PPR wheel
spun hard produces ~500 clicks/s and one message each would flood the link for
no gain - the sender cannot act on them faster than it dispatches anyway.

Tick rate is deliberately set well inside the sender's own jog interval. The
GrblHAL Sender dispatches proportional jogs every 75 ms; a scheduler here
running slower than that would add a second, unsynchronised quantiser on top,
and the two would compound into far more delay than either alone. At 50 Hz
this contributes ~10 ms average and the sender's existing coalescing does the
real work.

Jog cancel is sent once when motion stops, and again whenever the axis changes
mid-motion, so a partially executed jog on the old axis does not continue after
the operator has moved on.
"""

try:
    import protocol
except ImportError:
    from pendant import protocol


# 10 Hz. Raised from 50 Hz, then from 20 Hz, after machine testing.
#
# The binding constraint turned out not to be this scheduler at all. At 20 Hz
# the machine reported a kept ratio falling from 79% to 31% across one traverse,
# a lag peaking at 187 mm - about the depth of grblHAL's planner buffer - and
# finally a ten second gap in the status stream as the sender blocked. That is
# the controller being flooded: more jog blocks per second than it can parse,
# plan and execute, so its buffer fills and everything upstream stalls behind it.
#
# Halving the rate halves the blocks and doubles the distance each carries. The
# sender coalesces further on its own side, which is the real backpressure.
#
# A tick carries a whole number of detents, so at 20 ms and a fast wind a single
# detent is 17% of the message. With the planner buffer near empty, any tick
# that comes up one detent short leaves it nothing to execute and it
# decelerates - which is a continuous jerk through a long move, with grblHAL's
# reported feed oscillating while the pendant's commanded feed sits perfectly
# still. At 50 ms the same detent is 7% of a message three times the size.
#
# The latency cost is nil in practice: the sender dispatches its own jogs every
# 75 ms, so this was never the limiting quantiser.
TICK_MS = 100

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

# Millimetres of motion allowed to be in flight at once.
#
# This is, directly, how far the machine can still travel after the wheel stops.
# The scheduler tracks an estimate of the queue - adding what it commands,
# subtracting what the current feed drains each tick - and refuses to add beyond
# this.
#
# Bounding accumulated distance rather than per-tick distance matters: a single
# quick tick is harmless and must not be clipped, while sustained over-turning
# is exactly what banked up unboundedly and made run-on grow the longer the
# pendant was used.
#
# The surplus is dropped, not deferred. Turning faster than the machine can
# follow cannot be honoured - the choice is only between lagging and dropping,
# and a pendant that saves motion to replay after you stop is far worse than one
# that simply stops keeping up.

# Feed applied while a burst of motion establishes its buffer, and how long for.
#
# Commanding exactly the arrival rate holds the queue wherever it already is -
# and out of rest that is zero, so the planner has nothing in hand and runs dry
# on any tick that arrives light. The machine shows this as random jerks and
# occasional dead stops, and as motion that is smooth only when a ceiling
# happens to be binding, because a bound ceiling is what fills the queue.
#
# So under-feed briefly at the start of a burst to put something in the buffer,
# then track exactly. Since arrival and drain match after that, what was built
# stays built - no permanent loss, unlike trimming continuously.
#
# Timed rather than measured: the queue is read after the tick's drain, so it
# always presents at its trough and a measurement-driven version could never
# tell a filling buffer from a full one.
# Deliberately gentle. The trim toggles as the buffer crosses its target, so it
# is a feed change - and feed changes are what stop grblHAL blending. At 20% the
# toggle was a large step; at 5% it is 10 mm/s on a 200 mm/s move, which the
# machine absorbs in under 7 ms. The buffer fills more slowly and nothing else
# notices.
FEED_BUILD_TRIM = 0.95

# Buffer to keep in hand, as multiples of what the machine drains in a tick.
#
# The trace showed the queue reading exactly the distance emitted, every tick -
# drained and refilled, with nothing in reserve. A tick that arrives light then
# leaves the planner with nothing and the machine decelerates, which is the
# stumble. Human turning varies by a third tick to tick, so dips are constant
# and so were the stumbles.
#
# 1.5 ticks covers a dip of one full tick. It is also, directly, the run-on when
# the wheel stops - buffer and coasting are the same quantity, so this is the
# knob that trades one against the other, and it is the only bound on in-flight
# motion now that the regulator holds the depth.
BUFFER_TICKS = 1.5

# Hysteresis on the refill decision, as a fraction of the target either side.
#
# Without it the buffer crosses its target every tick - refill pushes it above,
# the next drain takes it below - so the trim toggles every tick and the feed
# alternates on every message. The wire showed exactly that: F2375, F2256.2,
# F2375, F2256.2, forever, which is 0.95 apart. grblHAL blends consecutive moves
# that share a feed, so alternating on every block means it can blend none of
# them and adjusts velocity between all of them.
#
# Refilling starts well below the target and stops well above, so the feed holds
# still through long runs and moves only when the buffer has genuinely drifted.
BUFFER_HYSTERESIS = 0.35


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
RATE_WINDOW_TICKS = 8

# Per-tick trace, for catching a stall in the act.
TRACE_TICKS = 24            # how much history to keep either side
TRACE_QUIET_TICKS = 100     # minimum gap between dumps, so one stall is one dump
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

        # True gives velocity-follow: stopping the wheel flushes queued motion
        # and halts, so the machine never runs on past your hand. False gives
        # queue-and-execute: every detent is honoured exactly, at the cost of
        # the machine lagging behind a fast spin and continuing after you stop.
        #
        # Exposed as a flag rather than a constant so the two can be compared
        # on a real machine from the REPL, without a reflash between runs.
        self.cancel_on_stop = True

        self.feed_tracking = FEED_TRACKING_ENABLED

        self._residual = 0
        self._idle_ticks = 0
        self._moving = False
        self._recent = []          # raw counts per tick, most recent last
        self.feed = FEED_MIN_MM_MIN   # last applied, for display
        self._queue_mm = 0.0          # estimate of motion in flight
        self._ticks_since_motion = 0  # interval used to measure turn rate
        self._building = True         # buffer still filling
        self._direction = 0           # sign of motion currently in flight
        self._cap_feed = FEED_MIN_MM_MIN  # untrimmed rate, for the emission cap

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
        """
        if not detents:
            return 0.0

        if not self._moving:
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
        elif target < FEED_MIN_MM_MIN:
            target = FEED_MIN_MM_MIN

        # What the machine drains at the untrimmed rate, kept for the emission
        # cap. The cap must not use the trimmed value: emitting exactly what a
        # reduced feed drains means nothing accumulates, so the buffer the trim
        # exists to build never appears and the trim just discards motion.
        self._cap_feed = target

        # Under-feed briefly at the start of a burst so the queue gains the
        # difference. Emission stays at the full rate, so what is commanded and
        # what is executed differ by exactly the trim - and that difference is
        # the buffer.
        # Under-feed whenever the buffer is below target, not just at the start
        # of a burst. Building it once and never topping it up meant the first
        # dip emptied it and it stayed empty - so the protection was gone
        # exactly when the move had been going long enough to need it.
        #
        # The queue is read here after this tick's drain, so this is the buffer
        # at its trough, which is the number that decides whether the planner
        # runs dry.
        wanted = (target / 60.0) * (TICK_MS / 1000.0) * BUFFER_TICKS
        if self._building:
            # Keep refilling until comfortably above target, not merely at it.
            if self._queue_mm > wanted * (1.0 + BUFFER_HYSTERESIS):
                self._building = False
        elif self._queue_mm < wanted * (1.0 - BUFFER_HYSTERESIS):
            self._building = True

        # Smooth towards the target rather than jumping to it - but only while
        # already moving. Starting from rest, smoothing would ramp up from the
        # floor over several ticks, and since the queue bound derives from the
        # feed it would also under-estimate capacity and drop detents at exactly
        # the moment the operator starts turning. Smoothing exists to damp
        # jitter during a turn, not to soften its start.
        if not self._moving:
            return target

        # Hold unless the target has left the band; otherwise take it exactly.
        if abs(target - self.feed) < self.feed * FEED_DEADBAND:
            settled = self.feed
        else:
            settled = target

        # The trim goes on after the deadband. Applied before it, a change this
        # small falls inside the band, is suppressed, and the buffer never
        # fills - the trim has to reach the commanded feed to do anything.
        if self._building:
            settled *= FEED_BUILD_TRIM
            if settled < FEED_MIN_MM_MIN:
                settled = FEED_MIN_MM_MIN
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
                self._building = True
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
            cap = self._cap_feed if self._building else self.feed
            allowed = int((cap / 60.0) * (TICK_MS / 1000.0) / self.step)
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

        if self._moving or self.feed > FEED_MIN_MM_MIN:
            # Let the smoothed feed fall while the wheel is still, or the first
            # detent after a pause would inherit the speed of the last burst.
            self.feed += FEED_DEADBAND * (FEED_MIN_MM_MIN - self.feed)
            if self.feed < FEED_MIN_MM_MIN:
                self.feed = FEED_MIN_MM_MIN

        if self._moving:
            if not self.cancel_on_stop:
                # Queue-and-execute: let the commanded motion finish on its own.
                self._moving = False
                self._idle_ticks = 0
                return None

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
        self.trace.append((round(self.feed), abs(detents), carried,
                           round(self._queue_mm, 1)))
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

        recent = [row[2] for row in self.trace[:-1]]
        average = sum(recent) / len(recent)
        if average <= 0:
            return

        # Only a dry queue while the operator is still turning is a fault. One
        # that empties because the wheel is slowing or stopped is the machine
        # doing as it was told, and flagging those buries the real ones.
        typical = sum(row[1] for row in self.trace[:-1]) / (len(self.trace) - 1)
        if abs(detents) < typical * 0.5:
            return

        self.stats["messages"] += 0          # no-op, keeps the counter honest
        self.stumbles += 1
        if self.stats["messages"] - self._last_dump < TRACE_QUIET_TICKS:
            return
        self._last_dump = self.stats["messages"]
        print("[starved {}] queue {:.2f} mm, carried {:.2f} against an average of {:.2f}".format(
            self.stumbles, self._queue_mm, carried, average))
        print("  feed  det   mm  queue")
        for feed, det, mm, queue in self.trace:
            print("  {:>5} {:>4} {:>5.2f} {:>5.1f}".format(feed, det, mm, queue))

    async def run(self, link):
        """Drive the scheduler forever, handing messages to the link."""
        import asyncio

        while True:
            message = self.tick()
            if message is not None:
                link.send(message)
            await asyncio.sleep_ms(TICK_MS)
