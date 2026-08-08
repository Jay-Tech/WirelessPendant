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


# 50 Hz. See module docstring: comfortably inside the sender's 75 ms dispatch
# interval, so this scheduler is not the bottleneck.
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
# Raised from 300 ms after machine testing: turning deliberately slowly left
# gaps longer than that, so it cancelled between individual detents - jog,
# cancel, jog, cancel - which reads as the machine refusing to move. Bounding
# the queue (below) is what makes a longer threshold safe: there is little
# backlog left for a late cancel to have to flush.
IDLE_MS_BEFORE_CANCEL = 600
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
FEED_SMOOTHING = 0.25

# Ceiling per step size, matching STEP_SIZES.
#
# The fine values are measured on the machine rather than derived - they came
# out roughly a third of what seemed reasonable on paper, which says a fine step
# wants control far more than speed.
#
# The coarse ones are the opposite problem. They were limited by a 5000 mm/min
# ceiling guessed before the machine's real limits were known; X and Y actually
# do 15000. Sized so a natural wind lands near the rate this machine is normally
# jogged at - around 9000 mm/min, which 1 mm reaches at 150 detents/s - with
# headroom above rather than running to the absolute maximum.
STEP_MAX_FEED = (150.0, 300.0, 950.0, 8000.0, 12000.0)

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
# In-flight motion is bounded to this many ticks' worth of travel at the
# current feed, rather than to a fixed distance.
#
# A constant is wrong because what it means changes with speed. At 1500 mm/s^2,
# stopping from F15000 takes 20.8 mm and from F1000 takes 0.09 mm - so 3 mm was
# simultaneously negligible at traverse and a large overshoot while creeping.
# Expressed in ticks it becomes a bounded amount of *time*, about 60 ms of
# motion, which is what the operator perceives as run-on.
QUEUE_TICKS = 3.0

# Rate is measured over a window rather than per tick: at 20 ms a tick sees one
# or two detents even during a fast spin, far too coarse to estimate speed from.
#
# Measured in raw quadrature counts rather than whole detents, which is four
# times the resolution for nothing. Counting detents gave a granularity of one
# detent per window - 5 detents/s, or a 300 mm/min jump at the 1 mm step - so
# the feed staircased instead of ramping. Counts plus a slightly longer window
# bring that to roughly 50 mm/min, which reads as smooth.
#
# The window doubles as the ramp: feed rises over its length when you start
# turning and falls over it when you stop, so a longer window is a gentler ramp
# as well as a finer measurement.
RATE_WINDOW_TICKS = 15


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
        self.stats = {"messages": 0, "detents": 0, "cancels": 0,
                      "dropped_detents": 0}

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
        """Detents per second, from the interval since the last movement.

        Measured as detents divided by the time they took, rather than averaged
        over a fixed window. A window is wrong in both directions: too slow to
        rise, so a fast start out-runs the feed and fills the queue, and too
        slow to fall, so winding down leaves the feed high while detents arrive
        ever slower - every move then finishes early and waits, which is felt as
        stutter and jerk as the wheel comes to rest.

        An interval is accurate at any speed and reacts within a single detent
        either way. terjeio's MPG firmware gets the same measurement from the
        PIO, which timestamps every edge.
        """
        if not detents:
            return 0.0
        seconds = self._ticks_since_motion * TICK_MS / 1000.0
        if seconds <= 0:
            seconds = TICK_MS / 1000.0
        return abs(detents) / seconds

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

        # Exactly the rate being commanded - no more.
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

        # Smooth towards the target rather than jumping to it - but only while
        # already moving. Starting from rest, smoothing would ramp up from the
        # floor over several ticks, and since the queue bound derives from the
        # feed it would also under-estimate capacity and drop detents at exactly
        # the moment the operator starts turning. Smoothing exists to damp
        # jitter during a turn, not to soften its start.
        if not self._moving:
            return target
        feed = self.feed + FEED_SMOOTHING * (target - self.feed)
        if feed < FEED_MIN_MM_MIN:
            feed = FEED_MIN_MM_MIN
        elif feed > ceiling:
            feed = ceiling
        return feed

    def tick(self):
        """Advance one interval. Returns a message to send, or None."""
        counts = self.encoder.take()

        # Drain the in-flight estimate by what the machine executes in a tick at
        # the feed last commanded. Done every tick, including idle ones, so the
        # queue empties while the wheel is still.
        self._queue_mm -= (self.feed / 60.0) * (TICK_MS / 1000.0)
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
            self._idle_ticks = 0

            # Feed is computed before _moving is set, because feed_rate reads
            # it to tell a fresh start from a continuing turn. Setting it first
            # made every start look like a continuation, so the feed smoothed up
            # from the floor - and since the queue bound derives from the feed,
            # that also discarded most of the opening detents.
            self.feed = self.feed_rate(detents)
            self._moving = True

            # Refuse to put more in flight than MAX_QUEUE_MM. The surplus is
            # dropped rather than carried in the residual, which would only
            # defer the same overshoot to the next tick.
            max_queue = (self.feed / 60.0) * (TICK_MS / 1000.0) * QUEUE_TICKS
            room = max_queue - self._queue_mm
            allowed = int(room / self.step)
            if allowed < 1:
                # Always let one detent through, or a full queue would stop
                # motion entirely. The drain still exceeds this, so the queue
                # continues to empty.
                allowed = 1
            if abs(detents) > allowed:
                self.stats["dropped_detents"] += abs(detents) - allowed
                detents = allowed if detents > 0 else -allowed
                self._residual = 0

            self._queue_mm += abs(detents) * self.step

            self._ticks_since_motion = 0
            self.stats["messages"] += 1
            self.stats["detents"] += abs(detents)
            return protocol.jog(self.axis, detents, self.step, self.feed)

        if self._moving or self.feed > FEED_MIN_MM_MIN:
            # Let the smoothed feed fall while the wheel is still, or the first
            # detent after a pause would inherit the speed of the last burst.
            self.feed += FEED_SMOOTHING * (FEED_MIN_MM_MIN - self.feed)
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
                self.stats["cancels"] += 1
                return protocol.jog_cancel()

        return None

    async def run(self, link):
        """Drive the scheduler forever, handing messages to the link."""
        import asyncio

        while True:
            message = self.tick()
            if message is not None:
                link.send(message)
            await asyncio.sleep_ms(TICK_MS)
