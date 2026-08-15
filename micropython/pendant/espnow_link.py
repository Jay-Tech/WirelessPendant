"""The pendant's half of the ESP-NOW link, behind link.py's own interface.

Drop-in for `link.PendantLink`: same send(), run(), connected, stats, flush()
and close(), so pendant.py chooses a transport and nothing else in it changes.
Both stay in the tree deliberately - the WiFi path works and is the fallback if
this disappoints, and having them side by side is what makes an A/B at the
machine one constant rather than a rewrite.

What is genuinely different, and why this is not just link.py with the sockets
swapped:

* **There is no connection.** Nothing to open, so nothing to report as open.
  `connected` becomes "a peer is known and has been heard from recently",
  which is a property this has to maintain rather than one the stack provides.
* **Discovery replaces addressing.** No host, no port, no DHCP, nothing in
  secrets.py. The pendant broadcasts until a receiver answers and then
  unicasts to whoever did.
* **Sends are acknowledged at the MAC layer.** ESP-NOW reports whether the
  packet reached the other radio, which a socket could never tell us. A
  failure here is unambiguously a link problem, where a missing reply over TCP
  might have been either end.
* **No association, so no channel negotiation.** Both boards drop any WiFi
  association and sit on the default channel. Two boards joined to different
  access points would never hear each other, and it would present as range.

Everything above the transport is unchanged: the same newline-terminated JSON,
one message per packet, the same LineDecoder, the same ping/pong.
"""

import asyncio
import time

import network
import espnow

try:
    import protocol
except ImportError:  # running as a package rather than from the board root
    from pendant import protocol


BROADCAST = b"\xff" * 6

# ESP-NOW's hard payload limit. Nothing the protocol sends approaches it - a
# jog message is around sixty bytes - but a longer one cannot go at all, and
# truncating would put malformed JSON on the wire rather than an honest drop.
MAX_PAYLOAD = 250

# How often to broadcast while looking for a receiver. Frequent enough that
# switching the pendant on feels immediate, sparse enough that a pendant left
# on with no receiver is not filling the air.
DISCOVERY_INTERVAL_MS = 500

PING_INTERVAL_S = 3

# Must clear several ping intervals, so one lost packet does not tear down a
# working link. Shorter than the TCP version's ten seconds: there is no
# association to re-establish here, so rediscovery costs a broadcast and a
# reply rather than a join, and being wrong is cheap.
RX_TIMEOUT_S = 5

# Deep enough to ride out a brief stall, short enough that anything still
# queued when the link returns is recent enough to be worth sending.
QUEUE_LIMIT = 32

# The radio is polled rather than waited on, because the same asyncio loop runs
# the jog scheduler and a blocking receive would hold it. Two milliseconds is a
# tenth of a jog tick, so it costs nothing measurable against a round trip an
# order of magnitude longer.
POLL_MS = 2

# A send taking this long or more is worth counting. Half a jog tick: at that
# point the call has cost the scheduler a turn, which is the unit that matters
# here rather than any absolute figure.
SLOW_TX_MS = 10


def log(msg):
    print("[{:>6}] {}".format(time.ticks_ms() // 1000, msg))


def mac_str(mac):
    return ":".join("%02X" % b for b in mac)


def start_radio():
    """Bring the radio up for ESP-NOW and return (espnow, own mac).

    Deliberately not joining anything. `active(True)` powers the radio;
    `disconnect()` sheds any association left from a previous run, because an
    associated station follows its access point's channel and the receiver -
    which has done the same - will be on the default.
    """
    sta = network.WLAN(network.STA_IF)
    sta.active(True)
    try:
        sta.disconnect()
    except Exception:
        pass

    # Power save parks the radio between beacons and put roughly a beacon
    # interval under every round trip on the WiFi path. There is no access
    # point here and so no beacons to park against, but the setting is a
    # property of the interface rather than of the association, so it is worth
    # clearing for the same reason and at the same cost.
    try:
        sta.config(pm=network.WLAN.PM_NONE)
    except Exception:
        pass

    e = espnow.ESPNow()
    e.active(True)
    e.add_peer(BROADCAST)
    return e, sta.config("mac")


class PendantLink:
    """A self-healing message link to the receiver at the sender's PC."""

    def __init__(self, on_message=None, queue_limit=QUEUE_LIMIT):
        self.on_message = on_message
        self.connected = False
        self.stats = {"sent": 0, "received": 0, "dropped": 0, "sessions": 0,
                      "unacked": 0,
                      # How often a send blocked long enough to cost the
                      # scheduler a tick, and the worst one seen. Only this
                      # transport sets these, so the WiFi path's report is
                      # unchanged.
                      "slow_tx": 0, "worst_tx_ms": 0}

        self._link = None
        self._peer = None
        self._queue = []
        self._limit = queue_limit
        self._decoder = protocol.LineDecoder()
        self._last_rx = 0
        # Last time a unicast send was acknowledged by the peer's radio.
        #
        # Liveness here is not "did anything arrive", which is what a socket
        # forces you to use. ESP-NOW acknowledges every unicast at the MAC
        # layer, so a successful send is direct proof the other radio is
        # listening - available even when the far end has nothing to say.
        #
        # Without this the link tore itself down every RX_TIMEOUT_S whenever
        # the sender was quiet, and then could not recover: rediscovery needs
        # the receiver to answer, and the receiver had already learned this
        # MAC and stopped answering. Seen on the bench as two hellos, a mode,
        # a ping, and then broadcasts forever.
        self._last_ack = 0
        self._ping_seq = 0

    # --- outbound ---------------------------------------------------------

    def send(self, message):
        """Queue a message, merging a jog into one already waiting.

        A jog carries a signed detent count and a step size, and the distance it
        means is exactly detents x step - so two jogs on the same axis with the
        same step add together with no approximation at all. Merging them loses
        nothing.

        Dropping did lose something. The old behaviour discarded the oldest
        message once the queue filled, detents and all, so movement the operator
        had turned for simply never reached the machine: better than a third of
        it on a fast jog, and the shortfall showed up as lag that never came
        back. It also arrived at the sender as a gap in the stream, which empties
        the controller's planner and stalls the axis - the drops and the stumble
        were the same event seen from two ends.

        This costs nothing when the link is keeping up, because a drained queue
        has no tail to merge with and messages stay fine-grained. It only takes
        effect once there is a backlog, which is exactly when the alternative was
        throwing motion away.

        Feed takes the larger of the two rather than the newer. It describes how
        fast the wheel is turning over the merged span, and the sender treats it
        as a ceiling it is free to lower - which it now does from its own
        measurement.
        """
        if message.get("t") == protocol.T_JOG and self._queue:
            tail = self._queue[-1]
            if (tail.get("t") == protocol.T_JOG
                    and tail.get("axis") == message.get("axis")
                    and tail.get("step") == message.get("step")):
                tail["det"] += message.get("det", 0)
                feed = message.get("feed")
                if feed is not None:
                    tail["feed"] = max(tail.get("feed", 0), feed)
                self.stats["merged"] = self.stats.get("merged", 0) + 1
                return

        if len(self._queue) >= self._limit:
            self._queue.pop(0)
            self.stats["dropped"] += 1
        self._queue.append(message)

    async def flush(self, timeout_ms=2000):
        """Wait for the outbound queue to drain. True if it emptied in time."""
        deadline = time.ticks_add(time.ticks_ms(), timeout_ms)
        while self._queue and self.connected:
            if time.ticks_diff(deadline, time.ticks_ms()) <= 0:
                return False
            await asyncio.sleep_ms(5)
        return not self._queue

    async def close(self):
        """Flush pending messages, then forget the peer."""
        await self.flush()
        self._drop_peer("closing")

    # --- peer -------------------------------------------------------------

    def _drop_peer(self, reason):
        if self._peer is not None:
            log("link down ({}) - sent={} received={} unacked={}".format(
                reason, self.stats["sent"], self.stats["received"],
                self.stats["unacked"]))
        self._peer = None
        self.connected = False
        # Anything queued now is stale operator input: detents from a wheel
        # that has since stopped. Replaying it when a receiver reappears would
        # act on intent the operator moved past a minute ago. Same reasoning as
        # the TCP path, which discards on reconnect for exactly this.
        dropped = len(self._queue)
        if dropped:
            self.stats["dropped"] += dropped
            self._queue = []
        self._decoder.reset()

    def _adopt_peer(self, host):
        try:
            self._link.add_peer(host)
        except OSError:
            pass                        # already registered
        self._peer = host
        self.connected = True
        self.stats["sessions"] += 1
        self._last_rx = time.ticks_ms()
        self._last_ack = self._last_rx
        log("receiver {} - link up".format(mac_str(host)))
        self._queue = [protocol.hello()]

    def _transmit(self, peer, payload):
        """One packet. Returns whether the other radio acknowledged it."""
        if len(payload) > MAX_PAYLOAD:
            self.stats["dropped"] += 1
            return True                 # not a link failure, so do not tear down
        # Timed, because how long this call takes is the open question about
        # this transport.
        #
        # ESPNow.send() is synchronous: it waits for the peer's radio to
        # acknowledge, and a packet that is not acknowledged first goes through
        # its retries. At the ~1% loss measured in the shop and fifty messages
        # a second, that is roughly one lost packet every two seconds - which
        # is the same order as the eleven loop stalls this transport reports
        # against WiFi's one. Suggestive, and not yet evidence.
        #
        # This is deliberately a measurement rather than a fix. The obvious
        # change - sync=False - was written once, bundled with two genuinely
        # bad changes, reverted untested, and would have been the fourth guess
        # in a row on this link.
        started = time.ticks_ms()
        try:
            acked = bool(self._link.send(peer, payload))
        except OSError:
            return False
        elapsed = time.ticks_diff(time.ticks_ms(), started)
        if elapsed >= SLOW_TX_MS:
            self.stats["slow_tx"] += 1
        if elapsed > self.stats["worst_tx_ms"]:
            self.stats["worst_tx_ms"] = elapsed
        # Only unicast to the current peer counts as evidence. A broadcast is
        # reported successful whether or not anything heard it, since there is
        # nobody specific to acknowledge it.
        if acked and peer == self._peer:
            self._last_ack = time.ticks_ms()
        return acked

    # --- the three loops --------------------------------------------------

    async def _rx(self):
        """Poll the radio and dispatch whatever arrives."""
        while True:
            try:
                host, message = self._link.recv(0)
            except OSError:
                host, message = None, None

            if message:
                if host != self._peer:
                    # Any packet is proof of a receiver, whether it is the
                    # answer to a broadcast or the first status of a session.
                    self._adopt_peer(host)
                self._last_rx = time.ticks_ms()

                for decoded in self._decoder.feed(message):
                    self.stats["received"] += 1
                    if decoded.get("t") == protocol.T_PING:
                        self.send(protocol.pong(decoded.get("seq", 0)))
                        continue
                    if self.on_message:
                        self.on_message(decoded)
            else:
                await asyncio.sleep_ms(POLL_MS)

    async def _tx(self):
        """Drain the queue to the peer, or broadcast until there is one."""
        next_ping = time.ticks_add(time.ticks_ms(), PING_INTERVAL_S * 1000)
        next_discovery = 0

        while True:
            if self._peer is None:
                # Broadcast carries a real hello rather than a bare probe, so
                # the receiver's first packet is already useful protocol
                # traffic and the sender sees the pendant arrive.
                if time.ticks_diff(next_discovery, time.ticks_ms()) <= 0:
                    self._transmit(BROADCAST, protocol.encode(protocol.hello()))
                    next_discovery = time.ticks_add(
                        time.ticks_ms(), DISCOVERY_INTERVAL_MS)
                await asyncio.sleep_ms(POLL_MS)
                continue

            if self._queue:
                message = self._queue.pop(0)
                if self._transmit(self._peer, protocol.encode(message)):
                    self.stats["sent"] += 1
                else:
                    # Unacknowledged means the packet never reached the other
                    # radio. Counted rather than retried: a stale jog resent is
                    # worse than a jog lost, and the deadline below will drop
                    # the peer if this is more than a glitch.
                    self.stats["unacked"] += 1
                continue

            if time.ticks_diff(next_ping, time.ticks_ms()) <= 0:
                self._ping_seq += 1
                self.send(protocol.ping(self._ping_seq))
                next_ping = time.ticks_add(
                    time.ticks_ms(), PING_INTERVAL_S * 1000)

            await asyncio.sleep_ms(POLL_MS)

    async def _deadline(self):
        """Forget the peer if nothing has arrived for RX_TIMEOUT_S."""
        while True:
            await asyncio.sleep_ms(500)
            if self._peer is None:
                continue
            # Whichever kind of evidence is more recent. A sender with nothing
            # to say leaves _last_rx stale while the peer is demonstrably
            # there, and tearing the link down for that is what produced two
            # hellos and then broadcasts forever on the bench - with
            # unacked=0 in the very message announcing the link was dead.
            now = time.ticks_ms()
            idle_ms = min(time.ticks_diff(now, self._last_rx),
                          time.ticks_diff(now, self._last_ack))
            if idle_ms > RX_TIMEOUT_S * 1000:
                self._drop_peer(
                    "no traffic and no acknowledgement for {}s".format(
                        RX_TIMEOUT_S))

    async def run(self):
        """Hold the link open forever, rediscovering as needed."""
        self._link, own_mac = start_radio()
        log("ESP-NOW up, this board is {}".format(mac_str(own_mac)))
        log("looking for a receiver - turn the wheel once one is found")

        await asyncio.gather(self._rx(), self._tx(), self._deadline())
