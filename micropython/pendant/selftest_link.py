"""End-to-end test of the pendant network link - no encoder or display needed.

Joins WiFi, connects to the mock sender (or the real one), then feeds it
synthetic handwheel motion: a sweep out and back on X, a Z move, and a couple
of button events. Verifies the round trip by checking the DRO the sender
reports back actually follows the jog commands sent.

Start the mock sender on the PC first:

    python tools/mock_sender.py

Then run this, with the PC's address:

    python -m mpremote connect id:7BE7DD09548134C0 run micropython/pendant/selftest_link.py

Set SENDER_HOST in secrets.py to avoid editing this file.
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

# 20 Hz is the rate the real jog scheduler will run at: fast enough to feel
# immediate, slow enough that each message carries several detents rather than
# flooding the link with one message per click.
JOG_HZ = 20
STEP_MM = 0.1

received = {"status": 0, "last": None}
failures = []


def check(label, ok, detail=""):
    print("  {:<44} {}{}".format(label, "PASS" if ok else "FAIL",
                                 "  " + detail if detail else ""))
    if not ok:
        failures.append(label)


def on_message(message):
    if message.get("t") == protocol.T_STATUS:
        received["status"] += 1
        received["last"] = message


async def sweep(pendant, axis, detents_per_tick, ticks):
    """Send `ticks` jog messages, returning the total detents sent."""
    total = 0
    for _ in range(ticks):
        pendant.send(protocol.jog(axis, detents_per_tick, STEP_MM))
        total += detents_per_tick
        await asyncio.sleep_ms(1000 // JOG_HZ)
    return total


async def wait_connected(pendant, timeout_s=25):
    deadline = time.ticks_add(time.ticks_ms(), timeout_s * 1000)
    while time.ticks_diff(deadline, time.ticks_ms()) > 0:
        if pendant.connected:
            return True
        await asyncio.sleep_ms(100)
    return False


async def settle(pendant, axis_index, expected, timeout_ms=3000):
    """Flush, then wait for the reported DRO to reach `expected`.

    Polling for convergence rather than sleeping a fixed interval: status
    arrives at 10 Hz and jog messages are still in flight when a sweep
    returns, so any fixed wait is a race that passes or fails on timing.
    """
    await pendant.flush()
    deadline = time.ticks_add(time.ticks_ms(), timeout_ms)
    while time.ticks_diff(deadline, time.ticks_ms()) > 0:
        last = received["last"]
        if last and abs(last["wpos"][axis_index] - expected) < 0.001:
            return True
        await asyncio.sleep_ms(50)
    return False


async def main():
    print("\npendant link self-test")
    print("=" * 46)

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

    # Let a couple of status frames land so we know the return path works.
    await asyncio.sleep_ms(400)
    check("status frames arriving", received["status"] > 0,
          "{} received".format(received["status"]))

    start = received["last"]["wpos"][0]

    # Out and back on X: net displacement should be zero.
    out = await sweep(pendant, "X", 3, 20)
    back = await sweep(pendant, "X", -3, 20)
    converged = await settle(pendant, 0, start)

    check("X sweep out and back returns to origin", converged,
          "{} -> {}".format(start, received["last"]["wpos"][0]))
    check("equal detents out and back", out + back == 0,
          "{:+d} then {:+d}".format(out, back))

    # A one-way Z move should land exactly where the arithmetic says.
    z_start = received["last"]["wpos"][2]
    z_detents = await sweep(pendant, "Z", -2, 10)
    expected = round(z_start + z_detents * STEP_MM, 3)
    converged = await settle(pendant, 2, expected)
    check("Z move lands at the expected position", converged,
          "{} -> {} (expected {})".format(
              z_start, received["last"]["wpos"][2], expected))

    pendant.send(protocol.jog_cancel())
    pendant.send(protocol.button("feed_hold", True))
    await asyncio.sleep_ms(150)
    pendant.send(protocol.button("feed_hold", False))

    check("nothing dropped from the send queue",
          pendant.stats["dropped"] == 0,
          "{} dropped".format(pendant.stats["dropped"]))
    check("link still up at the end", pendant.connected)
    check("single session throughout", pendant.stats["sessions"] == 1,
          "{} sessions".format(pendant.stats["sessions"]))

    # Flush before tearing down, or the trailing messages are discarded with
    # the socket and the far end never sees them.
    check("queue flushes cleanly before close", await pendant.flush())

    print("\n  sent={sent} received={received} dropped={dropped} "
          "sessions={sessions}".format(**pendant.stats))
    print("  status frames: {}".format(received["status"]))
    if received["last"]:
        print("  final DRO: {}".format(received["last"]["wpos"]))

    await pendant.close()
    runner.cancel()
    await asyncio.sleep_ms(100)

    print()
    if failures:
        print("{} FAILED: {}".format(len(failures), ", ".join(failures)))
        return 1
    print("all link tests passed")
    return 0


raise SystemExit(asyncio.run(main()))
