"""The boards this repo talks to, in one place.

Every tool resolves its device through here rather than carrying its own copy
of a serial number, so switching boards is a name rather than a paste.

    python tools/run_pendant.py --device pico
    python tools/sync_board.py --device esp32

Or point a whole session at one:

    set PICO_DEVICE=pico                    (Windows)
    export PICO_DEVICE=pico                 (POSIX)

A raw device string still works everywhere a name does, so an unlisted board
needs no edit here:

    set PICO_DEVICE=id:XXXXXXXXXXXXXXXX

Run it to see what is attached:

    python tools/board.py

Never use `mpremote connect auto`. It takes the first serial device it finds,
and on this bench that was the STM32 grblHAL controller - which then received a
raw-REPL handshake intended for a Pico. A CNC controller being sent arbitrary
bytes while a machine is powered is the one failure here with physical
consequences, which is why every tool targets an explicit ID and why the
listing below marks devices that are not Picos.
"""

import json
import os
import subprocess
import sys
import time

# The boards on this bench, by name. Serial numbers rather than COM ports: the
# ESP32-S3 presents different USB descriptors in MicroPython and in its ROM
# bootloader, so Windows renumbers it whenever it is reflashed, and a port that
# was right an hour ago is not.
BOARDS = {
    "pico": "id:7BE7DD09548134C0",      # Pico 2 W - the original pendant
    "esp32": "id:441BF6856C480000",     # Waveshare ESP32-S3-Touch-LCD-3.5
    "pendant": "id:441BF6856C480000",   # the same board, under the name that
                                        # says what it is rather than what
                                        # chip is on it
    "receiver2": "id:ACA7042DFB100000",  # ESP32-S3, the ESP-NOW end at the PC
    "receiver": "id:68EE8F50B2840000", # second one, on the bench, for testing
                                        # toward the production unit
}

# Names beginning "receiver" are the ESP-NOW end at the PC, and the prefix is
# load-bearing rather than descriptive: `sync_board.py --receiver` will only
# install receiver.py as main.py on a board named this way. Every ESP32-S3 here
# enumerates identically, so that name is the only guard against replacing the
# pendant's entry point with the receiver's - which fails silently, and later.
RECEIVER_PREFIX = "receiver"

# Which one a tool talks to when nothing says otherwise. Both are live: the
# ESP32 is where the pendant is going, and the Pico is the reference it gets
# compared against when something feels wrong.
DEFAULT_BOARD = "esp32"
DEFAULT_DEVICE = BOARDS[DEFAULT_BOARD]

# USB VID:PID pairs worth calling out when listing. Not an allowlist - anything
# unrecognised is simply unlabelled - but these are the ones that matter: two
# are what we want to talk to, one is what must never be talked to.
#
# Note that the pendant and the receiver are both ESP32-S3 and enumerate
# identically, so this label cannot tell them apart and neither can a human
# reading the list. Only the serial number does, which is the whole reason
# every tool here targets an ID.
KNOWN = {
    "2e8a:0005": "Raspberry Pi Pico (MicroPython)",
    "303a:4001": "ESP32-S3 (MicroPython)",
    "303a:1001": "ESP32-S3 (ROM bootloader - esptool, not mpremote)",
    "0483:5740": "STM32 - grblHAL CONTROLLER, DO NOT TARGET",
}

# The same descriptors again, named, because code that reasons about them
# should not be matching string literals against a display table. KNOWN is for
# printing; these are for deciding.
ROM_BOOTLOADER = "303a:1001"
MICROPYTHON_S3 = "303a:4001"
MICROPYTHON_PICO = "2e8a:0005"
CONTROLLER = "0483:5740"

# The only descriptors anything here may open a REPL against.
#
# An allowlist rather than a blocklist, and the difference is the whole point.
# Blocking the controller's descriptor would still leave every unrecognised
# device - a USB-serial adapter, a 3D printer, a modem - eligible for a raw-REPL
# handshake, and "unrecognised" is exactly what a controller behind a different
# USB bridge looks like. Nothing outside this tuple is opened at all.
TALKABLE = (MICROPYTHON_S3, MICROPYTHON_PICO)

# What a board says it is, written on the board itself at setup.
#
# The BOARDS table above is this bench's, and every entry in it is wrong for
# anyone else - which is the first thing a new builder hits, before any tool
# runs. A board that carries its own role needs no table entry, so setup.py
# writes one and resolution below falls back to reading it.
#
# It also makes the receiver guard enforceable. sync_board.py has to refuse to
# write receiver.py over the pendant's main.py, and until now the only thing it
# could check was a name in the local table - so a board absent from that table
# was unprotected. The marker travels with the hardware.
MARKER = "device.json"

PENDANT_ROLE = "pendant"
RECEIVER_ROLE = "receiver"
ROLES = (PENDANT_ROLE, RECEIVER_ROLE)


def device(explicit=None):
    """Resolve the device string: explicit argument, then env, then default.

    A board name is accepted anywhere a device string is, so `--device esp32`
    works as well as the serial number it stands for. PICO_DEVICE takes either
    too, which keeps `set PICO_DEVICE=pico` as a way to point a whole session
    at the other board.

    A role name that is not in BOARDS falls back to asking the attached boards
    which of them is that role. The table still wins where it has an entry, so
    this bench behaves exactly as before; the fallback is for a build where
    nobody has edited BOARDS, and it costs a device scan only in that case.
    """
    chosen = explicit or os.environ.get("PICO_DEVICE") or DEFAULT_DEVICE
    if chosen in BOARDS:
        return BOARDS[chosen]
    if chosen in ROLES:
        found = find_by_role(chosen)
        if found:
            return found
    return chosen


def port(explicit=None):
    """Resolve a board to an OS serial port, e.g. "COM19" or "/dev/ttyACM0".

    mpremote takes an `id:` string and finds the port itself. Anything else -
    pyserial, a terminal program - needs the port name, and hardcoding one
    reintroduces exactly the problem this module exists to avoid: COM numbers
    move when a board is reflashed, and both ESP32-S3s here look identical in
    a device listing.

    Returns None if the board is not attached, which the caller should report
    rather than fall back from - a bridge that quietly opened the wrong port
    would be talking to whatever else was plugged in.
    """
    target = device(explicit)
    if not target.startswith("id:"):
        return target                   # already a port name
    serial = target.split(":", 1)[1]
    for name, description in connected():
        if serial.lower() in description.lower():
            return name
    return None


def mpremote(dev, *args, capture=True):
    """Run mpremote against a device. Returns (returncode, output).

    Invoked as `python -m mpremote` rather than the bare executable because a
    pip install does not reliably put a launcher on PATH, and the failure mode
    is a confusing "not recognised" rather than anything pointing at mpremote.
    """
    command = [sys.executable, "-m", "mpremote", "connect", dev, *args]
    if not capture:
        return subprocess.run(command).returncode, ""
    result = subprocess.run(command, capture_output=True, text=True)
    return result.returncode, (result.stdout + result.stderr).strip()


def connected():
    """Serial devices mpremote can see, as a list of (id, description)."""
    result = subprocess.run(
        [sys.executable, "-m", "mpremote", "devs"],
        capture_output=True, text=True)
    if result.returncode != 0:
        return []
    found = []
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 3:
            found.append((parts[0], " ".join(parts[1:])))
    return found


def attached():
    """The same listing as connected(), but parsed into fields.

    `mpremote devs` prints "<port> <serial> <vid:pid> <manufacturer> <product>",
    and connected() flattens everything after the port into one string - which
    is why its callers match by substring. That is fine for a human-facing
    listing and no good for deciding whether a device may be opened, where
    "0483:5740 appears somewhere in this line" is a weaker test than it looks.

    Returns dicts with port, serial, vidpid and description. A device with no
    USB serial reports "0000:0000" and a serial of None, which is neither an
    error nor anything to talk to.
    """
    result = subprocess.run(
        [sys.executable, "-m", "mpremote", "devs"],
        capture_output=True, text=True)
    if result.returncode != 0:
        return []
    found = []
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        serial = parts[1]
        found.append({
            "port": parts[0],
            "serial": None if serial in ("None", "") else serial,
            "vidpid": parts[2].lower(),
            "description": " ".join(parts[3:]),
            "label": KNOWN.get(parts[2].lower(), ""),
        })
    return found


def talkable():
    """Attached boards it is safe to open a REPL against. See TALKABLE."""
    return [d for d in attached() if d["vidpid"] in TALKABLE]


def read_marker(dev):
    """Read the board's own account of what it is, or None if it has none.

    None is the ordinary answer for a board set up before this existed, so it
    means "unknown", never "wrong". Callers fall back to the BOARDS table.
    """
    script = (
        "try:\n"
        "    print(open('{}').read())\n"
        "except Exception:\n"
        "    print('')\n".format(MARKER)
    )
    code, output = mpremote(dev, "exec", script)
    if code != 0 or not output.strip():
        return None
    try:
        return json.loads(output.strip())
    except ValueError:
        # Truncated or hand-edited. Treated as absent rather than fatal: the
        # marker is a convenience, and a tool that refused to run because of a
        # damaged one would be worse than the table it replaced.
        return None


def write_marker(dev, role, extra=None):
    """Record on the board what it is. Returns True if it landed."""
    payload = {"role": role, "written": time.strftime("%Y-%m-%d")}
    payload.update(extra or {})
    # json.dumps then repr: the whole document goes across as one Python string
    # literal, so nothing in it has to survive a shell or mpremote's own
    # argument splitting.
    text = json.dumps(payload)
    script = "f=open('{}','w')\nf.write({})\nf.close()\n".format(MARKER, repr(text))
    code, _ = mpremote(dev, "exec", script)
    return code == 0


def find_by_role(role):
    """The attached board that says it is `role`, as an id: string.

    Returns None when none says so, and also when more than one does - an
    ambiguous answer must not be resolved by picking the first, which is the
    `connect auto` mistake wearing a different hat. The caller reports it and
    asks for --device.
    """
    matches = []
    for dev in talkable():
        if not dev["serial"]:
            continue
        marker = read_marker("id:" + dev["serial"])
        if marker and marker.get("role") == role:
            matches.append("id:" + dev["serial"])
    return matches[0] if len(matches) == 1 else None


def main():
    target = device()
    name = next((n for n, d in BOARDS.items() if d == target), None)
    print("target device: {}{}".format(
        target, "  ({})".format(name) if name else ""))
    if os.environ.get("PICO_DEVICE"):
        print("  (from PICO_DEVICE)")
    print()

    print("known boards:")
    for board_name, board_device in BOARDS.items():
        print("  {:<8} {}{}".format(
            board_name, board_device,
            "  <- default" if board_name == DEFAULT_BOARD else ""))
    print()

    devices = connected()
    if not devices:
        print("no serial devices found, or mpremote is not installed:")
        print("  python -m pip install --upgrade mpremote")
        return 1

    print("attached:")
    for port, description in devices:
        note = ""
        for vidpid, label in KNOWN.items():
            if vidpid in description.lower():
                note = "  <- {}".format(label)
        print("  {:<12} {}{}".format(port, description, note))

    serial = target.split(":", 1)[1] if ":" in target else target
    if not any(serial.lower() in d.lower() for _, d in devices):
        print()
        print("warning: {} is not among them.".format(target))
        print("  Every id in BOARDS above is this bench's. On a new setup they")
        print("  are all wrong: copy the ids from 'attached' into BOARDS, which")
        print("  fixes it for good. PICO_DEVICE only redirects one session.")
        print("  Either way, not `connect auto` - that targets whichever device")
        print("  is first, possibly the controller.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
