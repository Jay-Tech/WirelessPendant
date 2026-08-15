"""Network link from the pendant to the sender application.

Joins WiFi, holds a TCP session to the sender, and reconnects on its own. The
pendant is the client here: the sender is a fixed, always-on machine and the
pendant is the thing that wanders off, loses signal and gets switched off.

Three concerns this handles that a plain socket does not:

* **Silent death.** A TCP session whose peer vanished - PC asleep, AP dropped -
  stays open for minutes before the stack notices. On a pendant that reads as
  a handwheel that has simply stopped working. A ping every few seconds plus a
  receive deadline turns it into a fast, visible reconnect.
* **Backpressure.** The outbound queue is bounded and drops oldest. A pendant
  that cannot reach the sender must never grow a backlog of stale jog commands
  that all execute at once when the link returns.
* **Latency.** Nagle is disabled, for the same reason as in the serial bridge:
  single small messages are the entire traffic pattern here.
"""

import asyncio
import time

import network

try:
    import protocol
except ImportError:  # running as a package rather than from the board root
    from pendant import protocol


WIFI_JOIN_TIMEOUT_S = 20

PING_INTERVAL_S = 3
# Must clear several ping intervals so one lost packet on a congested 2.4 GHz
# channel does not tear down a working session.
RX_TIMEOUT_S = 10

# A connect that is refused fails at once; one whose SYN is silently dropped -
# a firewall, or an address with nothing at it - hangs instead. Without a bound
# the pendant sits there indefinitely saying nothing, which is the least useful
# way to fail.
CONNECT_TIMEOUT_S = 8

RECONNECT_DELAY_S = 1
RECONNECT_DELAY_MAX_S = 15

# Deep enough to ride out a brief stall, short enough that anything still
# queued when the link returns is recent enough to be worth sending.
QUEUE_LIMIT = 32


def log(msg):
    print("[{:>6}] {}".format(time.ticks_ms() // 1000, msg))


def wifi_connect(ssid, password, hostname=None):
    """Join WiFi and return the IP, or None. Safe to call when already up."""
    if hostname:
        network.hostname(hostname)

    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)

    # Power saving costs latency for no benefit while jogging, and it is on by
    # default: a fresh interface reads back PM_PERFORMANCE, whose low nibble is
    # 2 for PM2 power save. Left alone the radio parks between DTIM beacons and
    # puts roughly a beacon interval under every round trip. Measured round trip
    # bottoms out at 8 ms only because this call happens - see
    # selftest_latency.py.
    #
    # Failing quietly would cost about 90 ms per message and look like a network
    # problem, so it says so instead. Reported rather than raised: a pendant that
    # jogs slowly still jogs, and a port without the constant should not be a
    # pendant that will not start.
    #
    # Use the named constant, not the 0xa11140 that circulates for this. Both
    # disable power save - the low nibble is the mode, and both are 0 - but that
    # literal is a packed value from a different build's defaults, and only the
    # nibble is doing the work.
    try:
        wlan.config(pm=network.WLAN.PM_NONE)
    except Exception as exc:
        log("power save NOT disabled ({}: {}) - expect ~100 ms round trips"
            .format(type(exc).__name__, exc))

    if wlan.isconnected():
        return wlan.ifconfig()[0]

    log("joining {!r}...".format(ssid))
    wlan.connect(ssid, password)

    failures = {
        network.STAT_WRONG_PASSWORD: "wrong password",
        network.STAT_NO_AP_FOUND: "no such network in range",
        network.STAT_CONNECT_FAIL: "association failed",
    }

    deadline = time.ticks_add(time.ticks_ms(), WIFI_JOIN_TIMEOUT_S * 1000)
    while time.ticks_diff(deadline, time.ticks_ms()) > 0:
        status = wlan.status()
        if status == network.STAT_GOT_IP:
            ip = wlan.ifconfig()[0]
            log("online at {} ({} dBm)".format(ip, wlan.status("rssi")))
            return ip
        if status in failures:
            log("wifi failed: {}".format(failures[status]))
            return None
        time.sleep_ms(250)

    log("wifi join timed out")
    return None


class PendantLink:
    """A self-healing message link to the sender."""

    def __init__(self, host, port, on_message=None, queue_limit=QUEUE_LIMIT):
        self.host = host
        self.port = port
        self.on_message = on_message
        self.connected = False
        self.stats = {"sent": 0, "received": 0, "dropped": 0, "sessions": 0}

        self._queue = []
        self._limit = queue_limit
        self._decoder = protocol.LineDecoder()
        self._alive = False
        self._productive = False
        self._last_rx = 0
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
        """Wait for the outbound queue to drain. True if it emptied in time.

        Closing a socket discards whatever is still queued behind it, so any
        orderly shutdown - or any test that asserts on what the far end
        received - has to flush first. Without this, the last few messages
        before a close vanish silently, having already been counted as sent.
        """
        deadline = time.ticks_add(time.ticks_ms(), timeout_ms)
        while self._queue and self._alive:
            if time.ticks_diff(deadline, time.ticks_ms()) <= 0:
                return False
            await asyncio.sleep_ms(5)
        # The queue being empty only means the last write was handed to the
        # stack; give it a moment to reach the wire before anyone closes.
        await asyncio.sleep_ms(50)
        return not self._queue

    async def close(self):
        """Flush pending messages, then drop the session."""
        await self.flush()
        self._alive = False
        self.connected = False

    # --- session ----------------------------------------------------------

    def _enable_nodelay(self, writer):
        try:
            import socket

            sock = getattr(writer, "s", None)
            if sock is not None:
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except Exception:
            pass

    async def _tx(self, writer):
        next_ping = time.ticks_add(time.ticks_ms(), PING_INTERVAL_S * 1000)
        try:
            while self._alive:
                if self._queue:
                    message = self._queue.pop(0)
                    writer.write(protocol.encode(message))
                    await writer.drain()
                    self.stats["sent"] += 1
                    continue

                if time.ticks_diff(next_ping, time.ticks_ms()) <= 0:
                    self._ping_seq += 1
                    writer.write(protocol.encode(protocol.ping(self._ping_seq)))
                    await writer.drain()
                    self.stats["sent"] += 1
                    next_ping = time.ticks_add(
                        time.ticks_ms(), PING_INTERVAL_S * 1000)

                await asyncio.sleep_ms(5)
        except Exception as exc:
            log("tx failed: {}: {}".format(type(exc).__name__, exc))
        finally:
            # Drop the session so the reader unblocks instead of hanging on a
            # socket the writer has already given up on.
            self._alive = False
            try:
                writer.close()
            except Exception:
                pass

    async def _rx(self, reader):
        try:
            while self._alive:
                chunk = await reader.read(512)
                if not chunk:
                    log("sender closed the connection")
                    break
                self._last_rx = time.ticks_ms()
                if not self.connected:
                    # Only now is the link proven in both directions. A TCP
                    # connect can return before the handshake completes, so
                    # treating open_connection as "connected" reports a link
                    # that is about to be reset as working.
                    self.connected = True
                    self._productive = True
                    log("connected")
                for message in self._decoder.feed(chunk):
                    self.stats["received"] += 1
                    if message.get("t") == protocol.T_PING:
                        self.send(protocol.pong(message.get("seq", 0)))
                        continue
                    if self.on_message:
                        self.on_message(message)
        except Exception as exc:
            log("rx failed: {}: {}".format(type(exc).__name__, exc))
        finally:
            self._alive = False

    async def _deadline(self):
        """Tear the session down if nothing has arrived for RX_TIMEOUT_S.

        Catches the case a plain read cannot: the peer disappearing without
        closing, where the socket stays readable-but-silent indefinitely.
        """
        while self._alive:
            await asyncio.sleep_ms(500)
            idle_ms = time.ticks_diff(time.ticks_ms(), self._last_rx)
            if idle_ms > RX_TIMEOUT_S * 1000:
                # Counts included because the message alone cannot say whether
                # the far end went quiet or this end stopped reading. A session
                # that carried thousands of messages and then stopped is a
                # different fault from one that never carried any, and the
                # pendant's own watchdog says which side stalled.
                log("no traffic for {}s - assuming link is dead"
                    " (sent={} received={} queued={})".format(
                        RX_TIMEOUT_S, self.stats["sent"],
                        self.stats["received"], len(self._queue)))
                self._alive = False

    async def _session(self):
        log("connecting to {}:{}".format(self.host, self.port))
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port),
                CONNECT_TIMEOUT_S)
        except asyncio.TimeoutError:
            log("connect timed out after {}s - nothing answered at {}:{}".format(
                CONNECT_TIMEOUT_S, self.host, self.port))
            log("  a refused port fails immediately; a silent timeout means the")
            log("  SYN was dropped - firewall, or wrong address")
            raise OSError("connect timeout")
        self._enable_nodelay(writer)

        self._decoder.reset()
        self._alive = True
        self._last_rx = time.ticks_ms()
        self._productive = False
        self.stats["sessions"] += 1

        # Anything queued while the link was down is stale operator input: jog
        # detents from a wheel that has since stopped, buttons pressed a minute
        # ago. Replaying it on reconnect makes the machine act on intent the
        # operator has long moved past, so the queue starts empty.
        dropped = len(self._queue)
        if dropped:
            self.stats["dropped"] += dropped
            log("discarded {} message(s) queued while disconnected".format(
                dropped))
        self._queue = [protocol.hello()]

        tx = asyncio.create_task(self._tx(writer))
        deadline = asyncio.create_task(self._deadline())
        try:
            await self._rx(reader)
        finally:
            self._alive = False
            self.connected = False
            for task in (tx, deadline):
                try:
                    task.cancel()
                except Exception:
                    pass
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass
            log("disconnected")

    async def run(self):
        """Hold a session open forever, reconnecting with backoff."""
        delay = RECONNECT_DELAY_S
        while True:
            try:
                await self._session()
                # Only a session that actually carried traffic resets the
                # backoff. A connect that returns and is immediately reset
                # still completes _session normally, so resetting on that
                # alone would retry once a second forever against a host that
                # is up but has nothing listening.
                if self._productive:
                    delay = RECONNECT_DELAY_S
            except OSError as exc:
                log("connect failed: {}".format(exc))
            except Exception as exc:
                log("session error: {}: {}".format(type(exc).__name__, exc))

            self.connected = False
            await asyncio.sleep(delay)
            delay = min(delay * 2, RECONNECT_DELAY_MAX_S)
