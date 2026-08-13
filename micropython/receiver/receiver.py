"""ESP-NOW to USB serial bridge. The half of the link that sits at the PC.

The pendant stops joining the shop's WiFi and talks to this board directly over
ESP-NOW; this board is plugged into the sender's PC and appears as a serial
port. What crosses it is exactly what crossed the TCP socket before - the same
newline-delimited JSON - so the sender changes from a TCP listener to a serial
reader and the protocol itself does not change at all.

Deliberately knows nothing about that protocol. It moves complete lines and
never parses them, which means adding a message type, renaming a field or
changing the schema needs no reflash of this board. The only thing it
understands is where one line ends and the next begins.

    python -m mpremote connect <device> run micropython/receiver/receiver.py

Or copied to the board as main.py, which is how it should end up - this is a
device that wants to come up when the PC powers on, with nothing to launch.

Pairing is by discovery rather than configuration. The pendant broadcasts until
something answers; this board learns the pendant's MAC from the first packet it
receives and unicasts back to it. Nobody types a MAC address anywhere. What
that does not yet handle is two pendants in one shop - see PAIRING below.

Both ends must be on the same WiFi channel. An associated station follows its
access point's channel, so this board drops any association and stays on the
default, which is where a pendant that has done the same will be. Without that
step two boards on different APs never hear each other, and it presents as a
range problem rather than a configuration one.
"""

import sys
import select
import time

import network
import espnow


BROADCAST = b"\xff" * 6

# ESP-NOW's payload limit. A jog message is around sixty bytes, so this is not
# a constraint the protocol is anywhere near - but a line longer than this
# cannot be sent at all, and silently truncating one would corrupt the stream
# in a way the far end would read as malformed JSON rather than as too long.
MAX_PAYLOAD = 250

# How long to wait on the radio before going back to check the serial port.
# Short enough that outbound messages are not delayed behind it, long enough
# that the loop is not a spin - the sender dispatches every 10 ms and the
# pendant ticks every 20.
RECV_TIMEOUT_MS = 5

# Bytes to take from the serial port per pass. Bounded so a burst from the PC
# cannot starve the radio: whatever is left stays buffered and is picked up on
# the next pass, a few milliseconds later.
SERIAL_CHUNK = 256

# Longest line accepted from the PC before the buffer is abandoned. A stream
# that never produces a newline is something talking a different protocol at
# this port, and buffering it without limit would exhaust the heap. Matches
# the pendant's own LineDecoder.
MAX_LINE = 4096

# How long this board may be silent before it answers a packet with a hello.
#
# Only reached when the sender has nothing to say: its status frames arrive at
# 10 Hz and reset the timer, so during normal operation this never fires. When
# it does, it is both how a rebooted pendant re-pairs and a keepalive proving
# this end is still here.
HELLO_REPLY_MS = 2000


def mac_str(mac):
    return ":".join("%02X" % b for b in mac)


def note(message):
    """Say something on the data stream without corrupting it.

    Everything this board writes goes to the same serial port the sender is
    reading as protocol, so a bare print() would land mid-stream and read as a
    malformed line. Wrapping diagnostics as their own message type keeps the
    stream parseable - the protocol's rule is that unknown types are ignored,
    so a sender that has never heard of this one simply skips it, and a human
    watching the port sees something readable.
    """
    sys.stdout.buffer.write(
        b'{"t":"rx_note","msg":"' + message.encode() + b'"}\n')


def start_radio():
    """Bring up the radio for ESP-NOW and return (espnow, own mac)."""
    sta = network.WLAN(network.STA_IF)
    sta.active(True)
    try:
        sta.disconnect()
    except Exception:
        pass

    e = espnow.ESPNow()
    e.active(True)
    # Registered so the pendant's discovery broadcasts are heard. Unicast
    # replies go to the peer learned from the first packet, not to this.
    e.add_peer(BROADCAST)
    return e, sta.config("mac")


class Bridge:
    """Moves lines between the serial port and one ESP-NOW peer."""

    def __init__(self, espnow_link):
        self.link = espnow_link
        self.peer = None
        self.to_pc = 0
        self.to_pendant = 0
        self.unacked = 0
        self.oversize = 0
        self._last_tx = 0
        self._buffer = b""

    # --- pendant -> PC ----------------------------------------------------

    def pump_radio(self):
        """Forward one packet from the pendant to the serial port."""
        host, message = self.link.recv(RECV_TIMEOUT_MS)
        if not message:
            return

        if host != self.peer:
            # Learned rather than configured, and re-learned if it changes:
            # a pendant that reboots keeps its MAC, but a replacement board
            # will not, and requiring a reflash to pair a new pendant would
            # make this the fiddliest part of the build.
            try:
                self.link.add_peer(host)
            except OSError:
                pass                    # already registered
            self.peer = host
            note("pendant " + mac_str(host))

        # Answer when this board has been quiet, whether or not the peer is new.
        #
        # The pendant discovers by broadcasting, and broadcasts are not
        # acknowledged, so the only way it learns this board's address is by
        # receiving something from it. Answering only an unfamiliar MAC looked
        # sufficient and is not: a pendant that reboots keeps its MAC, so it
        # would broadcast at a receiver that already knew it, get no reply, and
        # never pair again until this board was restarted too.
        #
        # Sending nothing new during normal operation - the sender's status
        # frames reset this timer at 10 Hz - so this only speaks when the link
        # would otherwise be silent, where it doubles as a keepalive.
        if time.ticks_diff(time.ticks_ms(), self._last_tx) > HELLO_REPLY_MS:
            self.send(b'{"t":"rx_hello"}')

        sys.stdout.buffer.write(message)
        if not message.endswith(b"\n"):
            sys.stdout.buffer.write(b"\n")
        self.to_pc += 1

    # --- PC -> pendant ----------------------------------------------------

    def pump_serial(self, poller):
        """Forward whatever complete lines the PC has written."""
        if not poller.poll(0):
            return

        # Byte at a time because a poll that says "readable" promises one byte,
        # not a whole line, and a readline() here would block the radio until
        # the PC finished a sentence it might be halfway through. At the rates
        # involved - tens of short messages a second - this is free.
        for _ in range(SERIAL_CHUNK):
            byte = sys.stdin.buffer.read(1)
            if not byte:
                break
            if byte == b"\n":
                line, self._buffer = self._buffer, b""
                self.send(line)
            else:
                self._buffer += byte
            if not poller.poll(0):
                break

        if len(self._buffer) > MAX_LINE:
            self._buffer = b""
            note("oversize line from PC discarded")

    def send(self, line):
        """Send one line to the pendant, if there is one and it fits.

        The newline goes back on here. One packet carries exactly one
        newline-terminated line, which makes the wire format identical to what
        crossed the TCP socket - so the pendant's existing LineDecoder reads
        this with no change, and a decoder that needs a terminator to emit
        cannot sit holding a message forever.
        """
        if not line:
            return
        if not line.endswith(b"\n"):
            line += b"\n"
        if self.peer is None:
            # Nothing to send to yet. Dropped rather than queued: these are
            # status frames, and a status held until a pendant appears would
            # describe a machine state minutes stale by the time it arrived.
            return
        if len(line) > MAX_PAYLOAD:
            self.oversize += 1
            return
        try:
            # The return value is a MAC-layer acknowledgement, which TCP could
            # never give us: false means the packet did not reach the other
            # radio at all. Worth counting separately from silence, because one
            # is a link problem and the other could be the pendant.
            if self.link.send(self.peer, line):
                self.to_pendant += 1
                self._last_tx = time.ticks_ms()
            else:
                self.unacked += 1
        except OSError:
            self.unacked += 1


def main():
    link, own_mac = start_radio()
    bridge = Bridge(link)

    note("receiver up, this board is " + mac_str(own_mac))

    poller = select.poll()
    poller.register(sys.stdin, select.POLLIN)

    while True:
        bridge.pump_radio()
        bridge.pump_serial(poller)


raise SystemExit(main())
