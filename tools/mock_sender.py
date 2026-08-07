"""Stand-in for the sender application, for testing the pendant end to end.

Speaks the same protocol the real GrblHAL Sender will: accepts a pendant
connection, applies incoming jog messages to a simulated DRO, and streams
status back at 10 Hz. Turn the handwheel and the position here moves.

Exists so the pendant can be developed and tested against something before
the C# side has a listener - and afterwards, as a way to reproduce pendant
behaviour without a machine attached.

    python tools/mock_sender.py
    python tools/mock_sender.py --port 8422 --verbose
"""

import argparse
import asyncio
import json
import sys
import time

STATUS_HZ = 10

# Redraw rate for the on-screen DRO. Deliberately slower than the status rate:
# this is for a human to read, and digits changing 10 times a second are just a
# blur.
DRO_HZ = 5

AXES = ("X", "Y", "Z")


class MockSender:
    def __init__(self, verbose=False):
        self.verbose = verbose
        self.position = {axis: 0.0 for axis in AXES}
        self.state = "Idle"
        self.feed_override = 100
        self.spindle_override = 100
        self.peer = None
        self.counts = {"jog": 0, "btn": 0, "zero": 0, "ping": 0, "other": 0}
        self.jogged = 0.0
        self._started = time.time()

    # --- message handling -------------------------------------------------

    def handle(self, message, out):
        kind = message.get("t")

        if kind == "hello":
            print("  pendant says hello: {} v{}".format(
                message.get("dev"), message.get("ver")))
            return

        if kind == "jog":
            axis = message.get("axis")
            detents = message.get("det", 0)
            step = message.get("step", 0.0)
            if axis in self.position:
                distance = detents * step
                self.position[axis] += distance
                self.jogged += abs(distance)
                self.counts["jog"] += 1
                self.state = "Jog"
                if self.verbose:
                    print("  jog {} {:+d} det x {} = {:+.3f}".format(
                        axis, detents, step, distance))
            else:
                print("  ! jog for unknown axis {!r}".format(axis))
            return

        if kind == "jog_cancel":
            self.state = "Idle"
            if self.verbose:
                print("  jog cancel")
            return

        if kind == "zero":
            axis = message.get("axis")
            if axis in self.position:
                self.position[axis] = 0.0
                self.counts["zero"] += 1
                print("\n  ZERO {}".format(axis))
            else:
                print("\n  ! zero for unknown axis {!r}".format(axis))
            return

        if kind == "btn":
            self.counts["btn"] += 1
            print("  button {} {}".format(
                message.get("id"), "down" if message.get("down") else "up"))
            return

        if kind == "ping":
            self.counts["ping"] += 1
            out.append({"t": "pong", "seq": message.get("seq", 0)})
            return

        self.counts["other"] += 1
        print("  ? unhandled message: {}".format(message))

    def status(self):
        return {
            "t": "status",
            "state": self.state,
            "wpos": [round(self.position[a], 3) for a in AXES],
            "fro": self.feed_override,
            "sro": self.spindle_override,
        }

    def dro_line(self):
        return "{:<5} ".format(self.state) + "  ".join(
            "{} {:>9.3f}".format(axis, self.position[axis]) for axis in AXES)

    def summary(self):
        elapsed = max(time.time() - self._started, 0.001)
        return ("jog={jog} btn={btn} zero={zero} ping={ping} other={other}"
                "  moved={moved:.3f}mm  {rate:.1f} msg/s").format(
            moved=self.jogged,
            rate=sum(self.counts.values()) / elapsed,
            **self.counts)


async def handle_pendant(reader, writer, sender):
    peer = writer.get_extra_info("peername")
    peer = "{}:{}".format(*peer[:2]) if peer else "?"
    print("\npendant connected from {}".format(peer))
    sender.peer = peer

    buffer = b""
    last_status = 0.0
    last_dro = 0.0
    last_dro_text = ""

    try:
        while True:
            try:
                chunk = await asyncio.wait_for(reader.read(1024), timeout=0.05)
                if not chunk:
                    break
            except asyncio.TimeoutError:
                chunk = b""

            outbound = []
            if chunk:
                buffer += chunk
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    if not line.strip():
                        continue
                    try:
                        sender.handle(json.loads(line), outbound)
                    except ValueError:
                        print("  ! malformed line: {!r}".format(line[:80]))

            now = time.time()
            if now - last_status >= 1.0 / STATUS_HZ:
                last_status = now
                outbound.append(sender.status())

            # Live one-line DRO, rewritten in place. Without this the only way
            # to watch the pendant work is --verbose, which scrolls a line per
            # jog and is unreadable while the wheel is actually turning.
            if now - last_dro >= 1.0 / DRO_HZ:
                last_dro = now
                snapshot = sender.dro_line()
                if snapshot != last_dro_text:
                    last_dro_text = snapshot
                    print("\r" + snapshot + "   ", end="", flush=True)

            for message in outbound:
                writer.write((json.dumps(message) + "\n").encode())
            if outbound:
                await writer.drain()

    except (ConnectionResetError, BrokenPipeError):
        print("  connection reset by pendant")
    except Exception as exc:
        print("  session error: {}: {}".format(type(exc).__name__, exc))
    finally:
        print("pendant {} disconnected".format(peer))
        print("  {}".format(sender.summary()))
        print("  final position: " + "  ".join(
            "{}={:+.3f}".format(a, sender.position[a]) for a in AXES))
        sender.peer = None
        writer.close()


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8422)
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="log every jog message, not just buttons")
    args = parser.parse_args()

    sender = MockSender(verbose=args.verbose)
    server = await asyncio.start_server(
        lambda r, w: handle_pendant(r, w, sender), args.host, args.port)

    addresses = ", ".join(str(s.getsockname()) for s in server.sockets)
    print("mock sender listening on {}".format(addresses))
    print("point the pendant at this host on port {}".format(args.port))
    print("status at {} Hz; Ctrl-C to stop\n".format(STATUS_HZ))

    async with server:
        await server.serve_forever()


try:
    asyncio.run(main())
except KeyboardInterrupt:
    print("\nstopped.")
    sys.exit(0)
