"""Round-trip latency over ESP-NOW, measured the same way as the TCP link.

The point of this file is a number that sits beside selftest_latency.py's
without any argument about methodology: same sample count, same interval, same
percentiles, same reference feed. If the two disagree, it is the transport
disagreeing and not the measurement.

Deciding whether an independent link is worth building comes down to that
comparison. Over TCP on a dedicated 2.4 GHz channel, with the sender's own WiFi
hop in the path, the pendant measured:

    min 8   median 61   p90 90   p99 207   max 217 ms

ESP-NOW has no association, no DHCP and no beacons to wait on, so it should be
far below that. If it is not, the ceiling is the radio environment rather than
the infrastructure, and no amount of leaving the network behind will help.

Needs two ESP32s - any families, they interoperate. Flash this to both, with
ROLE changed on one:

    ROLE = "respond"    on the board that will sit at the PC
    ROLE = "ping"       on the board that will be the pendant

    python -m mpremote connect COM9 run micropython/pendant/selftest_espnow.py

Start the responder first. The pinger finds it by broadcast, so there are no MAC
addresses to type in.

Run it twice: once on the bench to see whether it works at all, then again at
the machine, which is the only environment whose answer counts.
"""

import network
import espnow
import time

# ---------------------------------------------------------------- settings --

ROLE = "respond"          # "respond" on one board, "ping" on the other

# Matched to selftest_latency.py so the two are directly comparable. 200 at
# 50 ms is ten seconds of wall clock - long enough for a tail to appear, short
# enough to repeat while walking around the shop.
SAMPLES = 200
INTERVAL_MS = 50

# Generous. ESP-NOW should answer in single-digit milliseconds; anything near
# this is a loss in all but name, and waiting longer only distorts the mean.
REPLY_TIMEOUT_MS = 500

# Only for the mm column, which exists to show how little of the scheduler's
# 13-17 mm lag transport accounts for.
REFERENCE_FEED_MM_MIN = 2500.0

BROADCAST = b"\xff" * 6

# What the TCP link measured, printed alongside so the comparison needs no
# notes. See selftest_latency.py.
TCP_BASELINE = "min 8   median 61   p90 90   p99 207   max 217 ms"


def mac_str(mac):
    return ":".join("%02X" % b for b in mac)


def start_radio():
    """Bring up the radio for ESP-NOW and return the interface."""
    sta = network.WLAN(network.STA_IF)
    sta.active(True)

    # Both ends of an ESP-NOW link have to sit on the same channel, and an
    # associated station follows whatever channel its access point chose.
    # Dropping the association leaves this board on the default, which is where
    # the other one will be as well. Without this, two boards joined to
    # different APs simply never hear each other and it looks like a range
    # problem.
    try:
        sta.disconnect()
    except Exception:
        pass

    print("this board is", mac_str(sta.config("mac")))

    e = espnow.ESPNow()
    e.active(True)
    e.add_peer(BROADCAST)
    return e


def deliver(e, peer, payload):
    """Send and report whether the peer acknowledged at the MAC layer.

    ESP-NOW acknowledges in hardware, which TCP could not tell us: a failure
    here means the packet never reached the other radio, where a missing reply
    might be either direction. Worth separating - one is a link problem and the
    other could be the responder.
    """
    try:
        return bool(e.send(peer, payload))
    except OSError:
        return False


# ------------------------------------------------------------- responder ----

def respond(e):
    print("\nresponding to anything that pings. Ctrl-C to stop.\n")
    known = set()
    replies = 0

    while True:
        host, msg = e.recv(1000)
        if host is None:
            continue

        if host not in known:
            try:
                e.add_peer(host)
            except OSError:
                pass                      # already registered
            known.add(host)
            print("  pinger:", mac_str(host))

        if deliver(e, host, msg):
            replies += 1
            if replies % 50 == 0:
                print("  {} replies".format(replies))
        else:
            print("  reply not acknowledged")


# ---------------------------------------------------------------- pinger ----

def find_responder(e):
    """Broadcast until something answers, then return its address."""
    print("\nlooking for a responder...")
    for _ in range(40):
        deliver(e, BROADCAST, b"hello")
        host, _msg = e.recv(250)
        if host is not None:
            try:
                e.add_peer(host)
            except OSError:
                pass
            print("found", mac_str(host))
            return host
    return None


def percentile(ordered, fraction):
    """Nearest-rank, matching selftest_latency.py - returns a value that was
    actually measured rather than an interpolation between two that were not."""
    if not ordered:
        return 0
    index = int(len(ordered) * fraction)
    if index >= len(ordered):
        index = len(ordered) - 1
    return ordered[index]


def as_mm(ms):
    return (REFERENCE_FEED_MM_MIN / 60.0) * (ms / 1000.0)


def ping(e):
    peer = find_responder(e)
    if peer is None:
        print("\nno responder. Is the other board running with ROLE = 'respond',")
        print("and did it come up before this one?")
        return 1

    print("\n{} pings at {} ms\n".format(SAMPLES, INTERVAL_MS))

    rtt_us = []
    unacked = 0        # never reached the other radio
    silent = 0         # acknowledged, but no reply came back
    late = 0           # answered, but not before the next ping went out

    for i in range(SAMPLES):
        # Drain anything still buffered before timing the next one.
        #
        # Without this a single late reply desynchronises the whole run: the
        # next recv() returns the *previous* ping's answer, which fails the
        # payload match, counts as silent, and leaves the stream permanently
        # off by one. Every sample after that mismatches while packets are
        # still flowing perfectly.
        #
        # It reads as sudden catastrophic loss and it is entirely an artefact.
        # Measured at the machine as 108/200 received with 90 "acknowledged but
        # never answered", against a responder that reported sending 200
        # replies with 5 unacknowledged - roughly 1% real loss reported as 46%.
        # That very nearly became a hardware conclusion.
        while True:
            _, stale = e.recv(0)
            if stale is None:
                break
            late += 1

        payload = b"p" + bytes([i & 0xFF])
        started = time.ticks_us()

        if not deliver(e, peer, payload):
            unacked += 1
            time.sleep_ms(INTERVAL_MS)
            continue

        host, msg = e.recv(REPLY_TIMEOUT_MS)
        if host is None or msg != payload:
            silent += 1
        else:
            rtt_us.append(time.ticks_diff(time.ticks_us(), started))

        time.sleep_ms(INTERVAL_MS)

    received = len(rtt_us)
    if not received:
        print("nothing came back at all - {} unacknowledged, {} silent".format(
            unacked, silent))
        return 1

    ordered = sorted(rtt_us)
    total = 0
    for value in ordered:
        total += value

    print("  {:<10} {:>8}  {:>9}".format(
        "", "ms", "mm @ {:.0f}".format(REFERENCE_FEED_MM_MIN)))
    for label, value_us in (
            ("min", ordered[0]),
            ("median", percentile(ordered, 0.50)),
            ("mean", total / received),
            ("p90", percentile(ordered, 0.90)),
            ("p99", percentile(ordered, 0.99)),
            ("max", ordered[-1])):
        ms = value_us / 1000.0
        print("  {:<10} {:>8.2f}  {:>9.3f}".format(label, ms, as_mm(ms)))

    print("\n  received {}/{}".format(received, SAMPLES))
    print("  {} never acknowledged by the peer's radio".format(unacked))
    print("  {} acknowledged but never answered".format(silent))
    # Reported separately because it is a different fault from loss and used
    # to masquerade as it. A reply that arrives after the next ping has gone
    # out is a tail-latency event, not a dropped packet, and the responder's
    # own count is the check: if it says it sent them, they were not lost.
    print("  {} answered after the next ping had gone out".format(late))

    print("\n  TCP for comparison:  {}".format(TCP_BASELINE))
    print("  Compare the tail. A median a few times better but the same p99")
    print("  means the ceiling is the 2.4 GHz environment, not the network")
    print("  stack - and an independent link will not move it.")
    return 0


# ------------------------------------------------------------------ main ----

def main():
    print("\nESP-NOW round trip  [role: {}]".format(ROLE))
    print("=" * 52)
    e = start_radio()

    if ROLE == "ping":
        return ping(e)
    if ROLE == "respond":
        respond(e)
        return 0

    print("ROLE must be 'ping' or 'respond', not {!r}".format(ROLE))
    return 1


raise SystemExit(main())
