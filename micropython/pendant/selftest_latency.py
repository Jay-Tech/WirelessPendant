"""Measures round-trip latency over the pendant's own link.

Exists to compare one radio against another - RP2350 with its CYW43439 on a
PIO-driven SPI bus, against an ESP32 with the radio on-die and lwIP in its own
FreeRTOS task - on the same network, against the same sender, with the same
code. Run it on both boards and the numbers are directly comparable.

What to read it for, and what not to:

The median is the least interesting figure. Both radios are wildly
over-provisioned for a 20 Hz stream of small messages, and neither is going to
struggle. What matters is the tail. A pendant that is usually quick and
occasionally 300 ms late feels worse than one that is uniformly slower,
because the hand has already moved on. p99 and max are the numbers to compare.

It is also not a measure of run-off. Lag as the scheduler reports it runs to
13-17 mm, which at 2500 mm/min is over a third of a second - two orders of
magnitude above anything here. That distance is planner depth and the
run-ahead policy, not the radio, so a better round trip will not shorten it.
The mm column below is printed to make that proportion obvious rather than to
suggest it is a target.

Start the mock sender on the PC first:

    python tools/mock_sender.py

Then, with the board's own port - never `connect auto`:

    python -m mpremote connect COM9 cp micropython/pendant/link.py :
    python -m mpremote connect COM9 cp micropython/pendant/protocol.py :
    python -m mpremote connect COM9 cp micropython/secrets.py :
    python -m mpremote connect COM9 run micropython/pendant/selftest_latency.py
"""

import asyncio
import time

try:
    import link
    import protocol
except ImportError:
    from pendant import link, protocol

import secrets

HOST = getattr(secrets, "SENDER_HOST", "192.168.1.193")
PORT = getattr(secrets, "SENDER_PORT", 8422)

# 200 samples at 50 ms is ten seconds of wall clock: long enough for the tail
# to show itself, short enough to run repeatedly while moving the pendant
# around the shop to find where the signal falls off.
SAMPLES = 200
INTERVAL_MS = 50

# link.py's own keepalive counts up from zero, one every three seconds, and
# its pongs come back on the same connection. Starting well above that keeps
# the two sets of sequence numbers from ever being confused for each other.
SEQ_BASE = 10000

# Only for the mm column, to show how little of the reported lag is transport.
REFERENCE_FEED_MM_MIN = 2500.0

sent_at = {}
rtt_ms = []


def on_message(message):
    if message.get("t") != protocol.T_PONG:
        return
    seq = message.get("seq", -1)
    started = sent_at.pop(seq, None)
    if started is not None:
        rtt_ms.append(time.ticks_diff(time.ticks_ms(), started))


def percentile(ordered, fraction):
    """Nearest-rank percentile. Exact enough at these sample counts, and it
    returns a value that was actually measured rather than an interpolation
    between two that were not."""
    if not ordered:
        return 0
    index = int(len(ordered) * fraction)
    if index >= len(ordered):
        index = len(ordered) - 1
    return ordered[index]


def as_mm(ms):
    """Travel at the reference feed during `ms`, for proportion only."""
    return (REFERENCE_FEED_MM_MIN / 60.0) * (ms / 1000.0)


async def wait_connected(pendant, timeout_s=25):
    deadline = time.ticks_add(time.ticks_ms(), timeout_s * 1000)
    while time.ticks_diff(deadline, time.ticks_ms()) > 0:
        if pendant.connected:
            return True
        await asyncio.sleep_ms(100)
    return False


async def main():
    print("\npendant link latency")
    print("=" * 52)

    ip = link.wifi_connect(
        secrets.WIFI_SSID, secrets.WIFI_PASSWORD,
        getattr(secrets, "HOSTNAME", None))
    if ip is None:
        print("  no network - aborting")
        return 1

    pendant = link.PendantLink(HOST, PORT, on_message=on_message)
    runner = asyncio.create_task(pendant.run())

    if not await wait_connected(pendant):
        print("\n  never connected to {}:{}".format(HOST, PORT))
        print("  is tools/mock_sender.py running on that host?")
        runner.cancel()
        return 1

    # Let the session settle before timing anything: the first message after a
    # connect pays for ARP and the initial window, which is real but is not
    # what a jogging session looks like.
    await asyncio.sleep_ms(500)

    print("  {} pings at {} ms, to {}:{}\n".format(
        SAMPLES, INTERVAL_MS, HOST, PORT))

    for i in range(SAMPLES):
        seq = SEQ_BASE + i
        sent_at[seq] = time.ticks_ms()
        pendant.send(protocol.ping(seq))
        await asyncio.sleep_ms(INTERVAL_MS)

    # Anything still in flight gets a moment to land before it counts as lost.
    await asyncio.sleep_ms(1000)

    received = len(rtt_ms)
    lost = SAMPLES - received
    if not received:
        print("  no pongs at all - does the sender answer pings?")
        await pendant.close()
        runner.cancel()
        return 1

    ordered = sorted(rtt_ms)
    total = 0
    for value in ordered:
        total += value

    print("  {:<10} {:>8}  {:>9}".format("", "ms", "mm @ {:.0f}".format(
        REFERENCE_FEED_MM_MIN)))
    for label, value in (
            ("min", ordered[0]),
            ("median", percentile(ordered, 0.50)),
            ("mean", total / received),
            ("p90", percentile(ordered, 0.90)),
            ("p99", percentile(ordered, 0.99)),
            ("max", ordered[-1])):
        print("  {:<10} {:>8.1f}  {:>9.2f}".format(
            label, value, as_mm(value)))

    print("\n  received {}/{}, lost {}".format(received, SAMPLES, lost))
    print("  link: sent={sent} received={received} dropped={dropped} "
          "sessions={sessions}".format(**pendant.stats))

    # A session count above one means the link tore down and rebuilt during a
    # ten second idle test, which is a far bigger finding than any percentile
    # above.
    if pendant.stats["sessions"] > 1:
        print("\n  *** the link reconnected mid-test - that is the result,")
        print("      whatever the latency figures say.")

    await pendant.close()
    runner.cancel()
    await asyncio.sleep_ms(100)

    print("\n  compare the tail, not the median: p99 and max are what a hand")
    print("  notices. The mm column is the share of the scheduler's 13-17 mm")
    print("  lag that transport actually accounts for.")
    return 0


raise SystemExit(asyncio.run(main()))
