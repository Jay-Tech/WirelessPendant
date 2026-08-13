"""Relay the ESP-NOW receiver's serial port to the sender's TCP port.

The step that lets the whole radio chain be judged against the **real** sender
before a line of C# changes. The pendant talks ESP-NOW to the receiver, the
receiver presents a serial port, and this carries those bytes to the port the
sender already listens on - so from the sender's side nothing is different and
from the pendant's side nothing is left of the network.

    py tools/espnow_bridge.py

Run it on the machine running the sender, so the TCP half is loopback and no
network is reintroduced by the thing meant to remove it.

    py tools/espnow_bridge.py --host 127.0.0.1 --port 8422
    py tools/espnow_bridge.py --device receiver --quiet

Temporary by design. Once PendantService reads the serial port directly this
has nothing left to do, and the fact that it is throwaway is the point: it
isolates "does ESP-NOW carry this protocol" from "does the sender read a
different transport", and those two questions should not fail together.

SAFETY. Once this is running the pendant can move the machine, with no window
of its own to notice. Start it deliberately, and Ctrl-C stops the relay - it
does not stop motion the controller has already queued.
"""

import argparse
import json
import socket
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import board  # noqa: E402

try:
    import serial  # noqa: E402
except ImportError:
    print("pyserial is missing:  py -m pip install pyserial")
    raise SystemExit(1)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8422

# The receiver is USB CDC, so this is ignored by the hardware. Stated anyway
# because pyserial requires it and a reader of this file should not have to
# wonder whether it matters.
BAUD = 115200

RECONNECT_DELAY_S = 2


def log(message):
    print("[bridge] {}".format(message), flush=True)


def is_receiver_note(line):
    """True for the receiver's own diagnostics rather than pendant traffic.

    The receiver writes its notes into the same stream, in its own `rx_`
    namespace, because a bare print would land mid-protocol. They are meant for
    whoever is watching this port, not for the sender - forwarding them would
    put lines in the sender's console that describe the transport it is
    deliberately unaware of.
    """
    try:
        return str(json.loads(line).get("t", "")).startswith("rx_")
    except (ValueError, TypeError, AttributeError):
        return False


def pump_serial_to_tcp(ser, sock, stop, quiet):
    """Pendant -> sender."""
    forwarded = 0
    while not stop.is_set():
        try:
            line = ser.readline()
        except Exception as exc:
            log("serial read failed: {}".format(exc))
            break
        if not line:
            continue
        text = line.strip()
        if not text:
            continue

        if is_receiver_note(text):
            if not quiet:
                log("receiver: {}".format(text.decode(errors="replace")))
            continue

        try:
            sock.sendall(text + b"\n")
        except OSError as exc:
            log("sender closed the connection ({})".format(exc))
            break
        forwarded += 1
        if not quiet and forwarded % 250 == 0:
            log("{} messages to the sender".format(forwarded))
    stop.set()


def pump_tcp_to_serial(ser, sock, stop, quiet):
    """Sender -> pendant."""
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
            log("sender closed the connection")
            break

        buffer += chunk
        # Split here rather than passing the chunk through, because the
        # receiver sends one ESP-NOW packet per write and a TCP read can carry
        # half a message or three of them. Handing it a fragment would put a
        # partial line on the radio.
        while b"\n" in buffer:
            line, buffer = buffer.split(b"\n", 1)
            if not line.strip():
                continue
            try:
                ser.write(line + b"\n")
            except Exception as exc:
                log("serial write failed: {}".format(exc))
                stop.set()
                return
    stop.set()


def session(ser, host, port, quiet):
    """One TCP session, held until either end goes away."""
    log("connecting to the sender at {}:{}".format(host, port))
    sock = socket.create_connection((host, port), timeout=8)
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    log("connected - the pendant can now move the machine")

    stop = threading.Event()
    threads = [
        threading.Thread(target=pump_serial_to_tcp,
                         args=(ser, sock, stop, quiet), daemon=True),
        threading.Thread(target=pump_tcp_to_serial,
                         args=(ser, sock, stop, quiet), daemon=True),
    ]
    for thread in threads:
        thread.start()
    try:
        while not stop.is_set():
            time.sleep(0.2)
    finally:
        stop.set()
        try:
            sock.close()
        except OSError:
            pass


def main():
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--device", default="receiver",
                        help="board name or serial port (default: receiver)")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--quiet", action="store_true",
                        help="suppress receiver notes and progress")
    args = parser.parse_args()

    name = board.port(args.device)
    if name is None:
        log("{} is not attached - `py tools/board.py` lists what is"
            .format(args.device))
        return 1

    log("receiver on {}".format(name))
    try:
        ser = serial.Serial(name, BAUD, timeout=0.2)
    except Exception as exc:
        log("could not open {}: {}".format(name, exc))
        log("  something else may hold it - a REPL, or mpremote still running")
        return 1

    # Reconnects rather than exiting, because the sender is the thing most
    # likely to be restarted during a session and having to restart this too
    # would make that annoying enough to avoid doing.
    try:
        while True:
            try:
                session(ser, args.host, args.port, args.quiet)
            except OSError as exc:
                log("connect failed: {}".format(exc))
            log("retrying in {}s (Ctrl-C to stop)".format(RECONNECT_DELAY_S))
            time.sleep(RECONNECT_DELAY_S)
    except KeyboardInterrupt:
        log("stopped - queued motion is still the controller's to finish")
    finally:
        ser.close()
    return 0


raise SystemExit(main())
