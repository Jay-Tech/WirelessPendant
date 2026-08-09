"""Wire protocol between the pendant and the sender application.

Newline-delimited JSON over TCP. One object per line, UTF-8, no framing header.
Chosen because the sender is C# and `System.Text.Json` reads this with no
custom parser, and because a human can read a capture with no tooling. At the
rates involved - jog updates in the tens per second, status at 10 Hz - the
encoding overhead is irrelevant next to the network round trip.

Every message carries `t` (type). Unknown types are ignored rather than
rejected, so either end can add messages without breaking the other.

Pendant -> sender:

    {"t":"hello","dev":"pico2w-pendant","ver":1}
    {"t":"jog","axis":"X","det":3,"step":0.1}     handwheel moved 3 detents
    {"t":"jog_cancel"}                            wheel stopped / axis changed
    {"t":"btn","id":"feed_hold","down":true}
    {"t":"ping","seq":42}

Sender -> pendant:

    {"t":"status","state":"Run","wpos":[10.5,20.0,-3.2],"fro":100,"sro":100}
    {"t":"pong","seq":42}

`jog` carries a detent count and a step size rather than a target position, so
a dropped message loses a little motion instead of desynchronising a position
the two ends would then disagree about. The sender multiplies det * step to get
the distance, which keeps step-size policy on the side that knows the machine.
"""

try:
    import json
except ImportError:  # pragma: no cover - CPython always has it
    import ujson as json

VERSION = 1
DEVICE = "pico2w-pendant"

# Pendant -> sender
T_HELLO = "hello"
T_JOG = "jog"
T_JOG_CANCEL = "jog_cancel"
T_BUTTON = "btn"
T_ZERO = "zero"
T_MODE = "mode"
T_PROBE = "probe"
T_PING = "ping"

# Sender -> pendant
T_STATUS = "status"
T_PONG = "pong"

AXES = ("X", "Y", "Z", "A")


def encode(message):
    """Serialise one message to a newline-terminated bytes object."""
    return (json.dumps(message) + "\n").encode()


def hello():
    return {"t": T_HELLO, "dev": DEVICE, "ver": VERSION}


def jog(axis, detents, step, feed=None):
    """A handwheel movement.

    `detents` is signed, `step` is mm per detent, and `feed` is mm/min matching
    the speed the wheel is being turned. Distance stays exactly detents x step;
    only the feed varies, so a dropped message costs a little motion rather than
    leaving the two ends disagreeing about position.
    """
    message = {"t": T_JOG, "axis": axis, "det": detents, "step": step}
    if feed is not None:
        message["feed"] = round(feed, 1)
    return message


def jog_cancel():
    return {"t": T_JOG_CANCEL}


def button(button_id, down):
    return {"t": T_BUTTON, "id": button_id, "down": bool(down)}


def zero(axis):
    """Set the work offset so the named axis reads zero here.

    Carries the axis explicitly rather than meaning "whatever is selected", so
    the sender never has to track pendant state to interpret it - and a message
    delayed by a reconnect cannot zero an axis the operator has since moved off.
    """
    return {"t": T_ZERO, "axis": axis}


def mode(axis, step):
    """Announce the pendant's selected axis, step size and jog behaviour.

    Axis and step never leave the pendant otherwise - they only change what
    future jog messages say - which leaves the sender unable to show the
    operator what the wheel is about to do. Publishing them costs one small
    message per change and makes the pendant's state visible where the
    operator is looking.
    """
    return {"t": T_MODE, "axis": axis, "step": step}


# Probe operations the pendant may ask for. Named rather than numbered so a
# mismatch between the two sides fails as an unknown operation the sender can
# name, rather than as the wrong cycle running.
PROBE_Z = "z"
PROBE_CORNER = "corner"
PROBE_TOOL_REFERENCE = "tlr"

# Distinct from PROBE_TOOL_REFERENCE, not a flag on it. One descends from
# wherever the tool is; the other traverses to a stored coordinate first, and
# two motions that different should not share a name.
PROBE_TOOL_REFERENCE_AT_SETTER = "tlr_setter"


def probe(operation):
    """Ask the sender to run a probe cycle.

    Only operations that make sense standing at the machine: a Z touch, a
    corner, and a tool length reference at the current position. Each is
    "position the tool, then probe" - which is the part the pendant is for.

    Deliberately carries no parameters. The sender owns those, per operation,
    and the pendant showing its own copy would be a second answer to a question
    that already has one - the same mistake as the shared rates that let a
    corner setup quietly rewrite the tool reference's numbers.
    """
    return {"t": T_PROBE, "op": operation}


def ping(seq):
    return {"t": T_PING, "seq": seq}


def status(state, wpos, feed_override=100, spindle_override=100):
    return {
        "t": T_STATUS,
        "state": state,
        "wpos": list(wpos),
        "fro": feed_override,
        "sro": spindle_override,
    }


def pong(seq):
    return {"t": T_PONG, "seq": seq}


class LineDecoder:
    """Reassembles newline-delimited JSON from arbitrary TCP chunk boundaries.

    TCP gives no message boundaries, so a read can land mid-object or carry
    several at once. Feed it whatever arrives; it yields complete messages.
    """

    # A well-formed message is far shorter than this. A stream that never
    # produces a newline is a peer talking a different protocol, and buffering
    # it without limit would exhaust the heap.
    MAX_LINE = 4096

    def __init__(self):
        self._buffer = b""
        self.overflows = 0
        self.malformed = 0

    def feed(self, chunk):
        """Return a list of complete messages decoded from `chunk`.

        Deliberately eager rather than a generator. A generator only advances
        while the caller iterates, so anyone who ignored the result - or broke
        out of the loop early - would silently skip both the buffering and the
        overflow guard below. Returning a list makes the side effects happen
        exactly once, when called.
        """
        messages = []
        if not chunk:
            return messages
        self._buffer += chunk

        while True:
            index = self._buffer.find(b"\n")
            if index < 0:
                break
            line = self._buffer[:index]
            self._buffer = self._buffer[index + 1:]
            if not line.strip():
                continue
            try:
                message = json.loads(line)
            except (ValueError, TypeError):
                self.malformed += 1
                continue
            if isinstance(message, dict) and "t" in message:
                messages.append(message)
            else:
                self.malformed += 1

        if len(self._buffer) > self.MAX_LINE:
            self.overflows += 1
            self._buffer = b""

        return messages

    def reset(self):
        self._buffer = b""
