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

import os
import subprocess
import sys

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
    "receiver": "id:ACA7042DFB100000",  # ESP32-S3, the ESP-NOW end at the PC
}

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


def device(explicit=None):
    """Resolve the device string: explicit argument, then env, then default.

    A board name is accepted anywhere a device string is, so `--device esp32`
    works as well as the serial number it stands for. PICO_DEVICE takes either
    too, which keeps `set PICO_DEVICE=pico` as a way to point a whole session
    at the other board.
    """
    chosen = explicit or os.environ.get("PICO_DEVICE") or DEFAULT_DEVICE
    return BOARDS.get(chosen, chosen)


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
        print("  Set PICO_DEVICE to one of the IDs above rather than reaching")
        print("  for `connect auto`, which would target whichever device is")
        print("  first - possibly the controller.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
