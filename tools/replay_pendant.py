"""Stream synthetic jog messages at the sender, with no pendant in the loop.

Answers one question: when the pendant reports half a metre of lag while the
controller's planner sits empty, where is that motion queued?

The pendant cannot answer it. Everything it measures is downstream of its own
radio, so a slow link and a slow sender look identical from the board. This
tool removes the radio, the encoder, the board and the network by connecting
from the PC and speaking the same protocol at the same rate.

    py tools/replay_pendant.py --host 192.168.1.184            (watch only)
    py tools/replay_pendant.py --host 192.168.1.184 --run      (commands motion)

If this reproduces the lag, the pendant and the link are both exonerated and
the fault is in the sender's dispatch path. If it comes back clean, the fault
is upstream of the sender and a board-to-board comparison is worth the rewire.

Metrics are computed exactly as pendant.py computes them - same lag formula,
same `bf` and `fr` fields - so the numbers sit side by side with the pendant's
own one-liner rather than needing translation.

SAFETY. This commands real motion on a real machine. It does nothing at all
without --run, and even then it jogs back and forth within --excursion so the
axis stays near where it started rather than traversing away from you. Start
with the machine clear of fixtures and keep a hand near feed hold.
"""

import argparse
import json
import socket
import sys
import threading
import time

# Matches jog.py TICK_MS. The point is to reproduce the pendant's cadence, so
# this tracks that constant rather than being tuned here.
TICK_MS = 20

AXIS_INDEX = {"X": 0, "Y": 1, "Z": 2, "A": 3}

DEFAULT_PORT = 8422


class Status:
    """What the sender tells us, guarded for the reader thread."""

    def __init__(self):
        self.lock = threading.Lock()
        self.wpos = None
        self.planner_free = 0
        self.planner_min = 999
        self.planner_max = 0
        self.actual_feed = 0
        self.frames = 0
        self.state = "?"


def reader(sock, status, stop):
    """Parse newline-delimited JSON until the socket closes."""
    buffer = b""
    sock.settimeout(0.5)
    while not stop.is_set():
        try:
            chunk = sock.recv(4096)
        except socket.timeout:
            continue
        except OSError:
            break
        if not chunk:
            break
        buffer += chunk
        while b"\n" in buffer:
            line, buffer = buffer.split(b"\n", 1)
            if not line.strip():
                continue
            try:
                message = json.loads(line)
            except ValueError:
                continue
            if message.get("t") != "status":
                continue
            with status.lock:
                status.frames += 1
                status.state = message.get("state", "?")
                wpos = message.get("wpos")
                if wpos:
                    status.wpos = wpos
                free = message.get("bf")
                if free is not None:
                    status.planner_free = free
                    if free < status.planner_min:
                        status.planner_min = free
                    if free > status.planner_max:
                        status.planner_max = free
                feed = message.get("fr")
                if feed is not None:
                    status.actual_feed = feed


def send(sock, message):
    sock.sendall((json.dumps(message) + "\n").encode())


def main():
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", required=True,
                        help="sender address, e.g. 192.168.1.184")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--run", action="store_true",
                        help="actually emit jog messages - THIS MOVES THE AXIS")
    parser.add_argument("--axis", default="X", choices=sorted(AXIS_INDEX))
    parser.add_argument("--step", type=float, default=0.1,
                        help="mm per detent (default 0.1)")
    parser.add_argument("--detents", type=int, default=6,
                        help="detents per tick, matching a brisk hand (6)")
    parser.add_argument("--excursion", type=float, default=10.0,
                        help="mm from the start point before reversing (10)")
    parser.add_argument("--seconds", type=float, default=15.0,
                        help="how long to stream (15)")
    args = parser.parse_args()

    # Sized from the arguments rather than assumed, because --detents and
    # --step are exactly what someone reaches for when the default does not
    # reproduce the fault, and a feed that did not follow them would quietly
    # change the thing being measured.
    feed = args.detents * args.step * (1000.0 / TICK_MS) * 60.0
    index = AXIS_INDEX[args.axis]

    print("connecting to {}:{}".format(args.host, args.port))
    sock = socket.create_connection((args.host, args.port), timeout=8)
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

    status = Status()
    stop = threading.Event()
    thread = threading.Thread(target=reader, args=(sock, status, stop),
                              daemon=True)
    thread.start()

    send(sock, {"t": "hello", "dev": "replay-pendant", "ver": 1})

    # Nothing is emitted until a status frame has been seen. Without one there
    # is no reference position, so lag would be measured against a guess - and
    # a tool whose whole purpose is a lag number should not invent its origin.
    deadline = time.time() + 5
    while time.time() < deadline:
        with status.lock:
            if status.wpos is not None:
                break
        time.sleep(0.05)

    with status.lock:
        if status.wpos is None:
            print("no status from the sender in 5s - is it connected to the"
                  " controller?")
            stop.set()
            return 1
        start_pos = status.wpos[index]
        print("sender is live: state={} wpos[{}]={:.3f} bf={} fr={}".format(
            status.state, args.axis, start_pos,
            status.planner_free, status.actual_feed))

    if not args.run:
        print("\nwatch-only. Streaming nothing. Re-run with --run to command"
              " motion:")
        print("  {} detents/tick of {} mm on {} = F{:.0f}, reversing every"
              " {} mm".format(args.detents, args.step, args.axis,
                              feed, args.excursion))
        stop.set()
        sock.close()
        return 0

    print("\nabout to jog {} back and forth within {} mm at F{:.0f}."
          .format(args.axis, args.excursion, feed))
    for count in (3, 2, 1):
        print("  {}...".format(count))
        time.sleep(1)

    commanded = 0.0
    direction = 1
    sent = 0
    peak_lag = 0.0
    lag = 0.0
    start_cmd = 0.0

    tick = TICK_MS / 1000.0
    end = time.time() + args.seconds
    next_report = time.time() + 1.0
    next_tick = time.time()

    try:
        while time.time() < end:
            now = time.time()
            if now < next_tick:
                time.sleep(min(next_tick - now, tick))
                continue
            next_tick += tick

            # Reverse at the excursion bound rather than accumulating travel in
            # one direction. Half a metre of run-off is the fault being chased;
            # a tool that could produce it as a straight traverse would be the
            # same hazard with a different cause.
            if abs(commanded) >= args.excursion:
                direction = -direction

            detents = args.detents * direction
            send(sock, {"t": "jog", "axis": args.axis, "det": detents,
                        "step": args.step, "feed": round(feed, 1)})
            commanded += detents * args.step
            sent += 1

            with status.lock:
                if status.wpos is not None:
                    travelled = status.wpos[index] - start_pos
                    lag = abs((commanded - start_cmd) - travelled)
                    if lag > peak_lag:
                        peak_lag = lag

            if time.time() >= next_report:
                next_report += 1.0
                with status.lock:
                    print("  sent={:<5} cmd={:+8.2f}mm  act={:+8.2f}mm  "
                          "lag={:.1f}/{:.1f}mm  bf={:<3} fr={:<5} frames={}"
                          .format(sent, commanded,
                                  status.wpos[index] - start_pos,
                                  lag, peak_lag, status.planner_free,
                                  status.actual_feed, status.frames))
    except KeyboardInterrupt:
        print("\ninterrupted")
    except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
        # Almost always the pendant, not a fault. PendantService accepts one
        # client and the newest wins, so a running pendant displaces this tool
        # and its auto-reconnect displaces it back a second later - which
        # arrives here as a reset a handful of messages in, and reads like a
        # network problem rather than the two of them taking turns.
        print("\nconnection dropped after {} message(s).".format(sent))
        if sent < 20:
            print("  That is almost certainly the pendant reconnecting and"
                  " taking the slot back.")
            print("  The sender accepts one client, newest wins. Stop the"
                  " pendant and re-run.")
    finally:
        try:
            send(sock, {"t": "jog_cancel"})
        except OSError:
            pass
        # The machine is still running out whatever is queued, and that
        # backlog is the measurement. Watching it drain says how much of the
        # lag was real distance rather than a late status report.
        print("\ncancelled - watching the machine run out...")
        settle = time.time() + 3
        while time.time() < settle:
            time.sleep(0.25)
        with status.lock:
            travelled = status.wpos[index] - start_pos
            print("\nafter settling:")
            print("  commanded  {:+.2f} mm".format(commanded))
            print("  actual     {:+.2f} mm".format(travelled))
            print("  shortfall  {:+.2f} mm".format(commanded - travelled))
            print("  peak lag   {:.1f} mm".format(peak_lag))
            print("  planner free  min {}  max {}".format(
                status.planner_min, status.planner_max))
            print("  status frames {} in {:.0f}s".format(
                status.frames, args.seconds))
        stop.set()
        sock.close()

    print("\nA planner that never fell below its maximum means the controller"
          " sat idle\nwhile the sender held the work. A shortfall that stays"
          " after settling means\nmessages were lost rather than delayed.")
    return 0


raise SystemExit(main())
