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
# 5 mm is suspended for now. Feed has to be at least rate x step x 60 to avoid
# lagging, so a coarse step saturates the machine's ceiling at a very low turn
# rate - 5 mm reaches 5000 mm/min at 17 detents/s, which is barely turning, and
# everything above that just pins at maximum with no proportional feel left.
STEP_SIZES = (0.001, 0.01, 0.1, 1.0)
DEFAULT_STEP_INDEX = 2

# Silence that counts as "the operator stopped" rather than "turning slowly".
#
# Cancel flushes queued motion, which is the point - stopping the wheel should
# halt the machine rather than let it run on through a backlog. The risk is
# firing during a slow turn and truncating a jog mid-move. Someone winding
# continuously produces a detent at least every ~300 ms even when crawling, so
# anything quieter than that is a genuine stop. Shorter thresholds cancel
# between individual detents during normal slow jogging.
IDLE_MS_BEFORE_CANCEL = 300
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

# Floor, so a slow careful turn still moves at a usable rate rather than
# crawling, and ceiling, which should sit at or below the machine's own maximum
# jog rate - asking for more than it can deliver just rebuilds the queue this
# is meant to avoid.
FEED_MIN_MM_MIN = 100.0
FEED_MAX_MM_MIN = 5000.0

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
        self.stats = {"messages": 0, "detents": 0, "cancels": 0}

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
        self.encoder.take()  # discard motion that arrived during the switch
        if self._moving:
            self._moving = False
            self._idle_ticks = 0
            self.stats["cancels"] += 1
            return protocol.jog_cancel()
        return None

    # --- per tick ---------------------------------------------------------

    def turn_rate(self):
        """Detents per second, averaged over the full window.

        Divided by the whole window rather than by however many samples exist
        yet, so a burst at the very start of a turn averages in instead of
        reading as a sustained sprint. Accumulated in quadrature counts and
        converted at the end, which is what gives the sub-detent resolution.
        """
        total = 0
        for value in self._recent:
            total += abs(value)
        seconds = RATE_WINDOW_TICKS * TICK_MS / 1000.0
        return (total / COUNTS_PER_DETENT) / seconds

    def feed_rate(self):
        """Feed in mm/min that matches the current winding speed.

        detents/s x mm/detent x 60 is exactly the rate the operator is asking
        for, so the machine tracks the hand rather than lagging behind it or
        racing ahead. Clamped at both ends: a floor so a careful turn is not
        glacial, and a ceiling because commanding more than the machine can
        deliver only rebuilds the queue.
        """
        if not self.feed_tracking:
            return FEED_MAX_MM_MIN
        commanded = self.turn_rate() * self.step * 60.0
        if commanded < FEED_MIN_MM_MIN:
            return FEED_MIN_MM_MIN
        if commanded > FEED_MAX_MM_MIN:
            return FEED_MAX_MM_MIN
        return commanded

    def tick(self):
        """Advance one interval. Returns a message to send, or None."""
        counts = self.encoder.take()

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
            self._moving = True

            # Distance stays exactly one step per detent. Only the feed moves.
            self.feed = self.feed_rate()

            self.stats["messages"] += 1
            self.stats["detents"] += abs(detents)
            return protocol.jog(self.axis, detents, self.step, self.feed)

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
