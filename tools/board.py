"""The board this repo talks to, in one place.

Every tool that reaches the Pico resolves the device through here rather than
carrying its own copy of the serial number, so changing board means changing
one line or setting one environment variable.

    set PICO_DEVICE=id:XXXXXXXXXXXXXXXX     (Windows)
    export PICO_DEVICE=id:XXXXXXXXXXXXXXXX  (POSIX)

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

# This bench's Pico 2 W. Override with PICO_DEVICE rather than editing, unless
# the board has been replaced for good.
DEFAULT_DEVICE = "id:7BE7DD09548134C0"

# USB VID:PID pairs worth calling out when listing. Not an allowlist - anything
# unrecognised is simply unlabelled - but these two are the ones that matter:
# one is what we want to talk to, the other is what must never be talked to.
KNOWN = {
    "2e8a:0005": "Raspberry Pi Pico (MicroPython)",
    "0483:5740": "STM32 - grblHAL CONTROLLER, DO NOT TARGET",
}


def device(explicit=None):
    """Resolve the device string: explicit argument, then env, then default."""
    return explicit or os.environ.get("PICO_DEVICE") or DEFAULT_DEVICE


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
    print("target device: {}".format(target))
    if os.environ.get("PICO_DEVICE"):
        print("  (from PICO_DEVICE)")
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
