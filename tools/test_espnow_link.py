"""Tests for the ESP-NOW link's send path. Runs on a PC with a fake radio.

    python tools/test_espnow_link.py

What these hold is one rule: sending must not block the asyncio loop on a
receiver that has stopped answering.

ESPNow.send() waits for the peer's radio to acknowledge, and an unacknowledged
packet burns a full retry sequence first. That call runs inside the event loop,
so against a wedged receiver at fifty messages a second nothing else is ever
scheduled - not the encoder, not touch, and not _deadline, which is the very
watchdog that would have dropped the peer and ended it. The display holds its
last frame because nothing redraws it. Seen at the machine as a pendant frozen
with its UI still up, needing a power cycle, every time the receiver was
disturbed.

The ping is the deliberate exception. _last_ack is the only liveness this link
has while the sender has nothing to say - which is its normal state until it
adopts the pendant - and without it the link tore itself down every RX_TIMEOUT_S
and could not recover. One waited send every PING_INTERVAL_S cannot starve the
loop.
"""

import sys
import time
import types
from pathlib import Path

for name in ("machine", "network", "espnow"):
    sys.modules.setdefault(name, types.ModuleType(name))

# MicroPython's monotonic tick helpers, which espnow_link uses throughout.
time.ticks_ms = lambda: int(time.monotonic() * 1000)
time.ticks_add = lambda t, d: t + d
time.ticks_diff = lambda a, b: a - b

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "micropython"))

from pendant import protocol  # noqa: E402
from pendant.espnow_link import (PendantLink, waits_for_ack,  # noqa: E402
                                 RECV_ERROR_LIMIT)

PEER = b"\x01\x02\x03\x04\x05\x06"

failures = []


def check(label, got, want):
    if got != want:
        failures.append("{}: got {!r}, wanted {!r}".format(label, got, want))


class FakeRadio:
    """Records how each packet was sent, and whether it was acknowledged."""

    def __init__(self, acked=True):
        self.acked = acked
        self.calls = []

    def send(self, peer, payload, sync=True):
        self.calls.append({"peer": peer, "sync": sync})
        return self.acked

    def add_peer(self, peer):
        pass


def link_with(radio):
    link = PendantLink()
    link._link = radio
    link._peer = PEER
    link.connected = True
    return link


# --- which messages wait ---------------------------------------------------

check("ping waits", waits_for_ack(protocol.ping(1)), True)
check("jog does not wait",
      waits_for_ack(protocol.jog("X", 3, 1.0, 2000)), False)
check("jog cancel does not wait", waits_for_ack(protocol.jog_cancel()), False)
check("hello does not wait", waits_for_ack(protocol.hello()), False)
check("button does not wait", waits_for_ack(protocol.button("feed_hold", True)),
      False)

# --- the flag reaches the radio -------------------------------------------

radio = FakeRadio()
link = link_with(radio)

link._transmit(PEER, b"{}")
check("default send does not wait", radio.calls[-1]["sync"], False)

link._transmit(PEER, b"{}", True)
check("ping send waits", radio.calls[-1]["sync"], True)

# --- only a waited, acknowledged send is evidence about the far end --------

radio = FakeRadio(acked=True)
link = link_with(radio)
link._last_ack = 0

link._transmit(PEER, b"{}")
check("an unwaited send proves nothing about the peer", link._last_ack, 0)

link._transmit(PEER, b"{}", True)
if link._last_ack == 0:
    failures.append("a waited, acknowledged send should refresh _last_ack")

# A radio that refuses the packet must not look like proof of life, or the
# deadline never fires and the peer is never dropped.
radio = FakeRadio(acked=False)
link = link_with(radio)
link._last_ack = 0
link._transmit(PEER, b"{}", True)
check("an unacknowledged send proves nothing", link._last_ack, 0)

# A broadcast is reported successful whether or not anything heard it.
radio = FakeRadio(acked=True)
link = link_with(radio)
link._last_ack = 0
link._transmit(b"\xff" * 6, b"{}", True)
check("a broadcast is not proof of a peer", link._last_ack, 0)

# --- the radio must never be able to end the loop --------------------------
#
# The fault that was caught on the machine, as a traceback rather than a
# theory:
#
#     File "espnow_link.py", line 342, in _rx
#     File "espnow.py", line 20, in recv
#     ValueError: ESPNow.recv(): buffer error
#
# _rx caught OSError only. ESPNow.recv() raises ValueError when its receive
# ring is left inconsistent - which is what a receiver resetting
# mid-transmission produces at this end - so the exception went straight
# through, asyncio.gather cancelled _tx and _deadline with it, and run()
# returned. The pendant froze with its UI still drawn, unrecoverable without a
# power cycle.

import asyncio  # noqa: E402

asyncio.sleep_ms = lambda ms: asyncio.sleep(ms / 1000.0)


class ExplodingRadio:
    """A radio whose recv always fails the way the real one did.

    Stops the loop by call count rather than by clock. A timed run is at the
    mercy of the host event loop's timer granularity - on Windows that is about
    15 ms against a 2 ms poll, which gave a fifth of the iterations the
    escalation needed and failed for reasons that had nothing to do with the
    code under test.
    """

    def __init__(self, exc, stop_after):
        self.exc = exc
        self.stop_after = stop_after
        self.calls = 0

    def recv(self, timeout):
        self.calls += 1
        if self.calls > self.stop_after:
            # BaseException in CPython, so the loop's `except Exception` does
            # not swallow it and the run ends where the test says it does.
            raise asyncio.CancelledError
        raise self.exc

    def send(self, peer, payload, sync=True):
        return True

    def add_peer(self, peer):
        pass


def run_until_stopped(link):
    """Run _rx until the radio stops it. Anything else escaping is a failure."""

    async def go():
        try:
            await link._rx()
        except asyncio.CancelledError:
            return True          # reached the end under its own steam
        return False             # returned on its own, which it must not

    return asyncio.new_event_loop().run_until_complete(go())


for error in (ValueError("ESPNow.recv(): buffer error"),
              RuntimeError("something nobody predicted")):
    name = type(error).__name__
    radio = ExplodingRadio(error, stop_after=RECV_ERROR_LIMIT + 5)
    link = link_with(radio)

    check("loop survives {}".format(name), run_until_stopped(link), True)

    if link.stats["recv_errors"] < RECV_ERROR_LIMIT:
        failures.append("{}: only {} failures counted".format(
            name, link.stats["recv_errors"]))

    # A ring that will not clear is not something the loop can talk its way out
    # of, so the peer is dropped and discovery starts over rather than the
    # pendant failing forever against a peer that cannot answer.
    check("{}: peer dropped after the limit".format(name), link._peer, None)

# --- reporting -------------------------------------------------------------

if failures:
    print("FAILED")
    for line in failures:
        print("  " + line)
    sys.exit(1)

print("espnow link send path: all checks passed")
