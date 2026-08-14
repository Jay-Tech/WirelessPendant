"""Copy the pendant modules and secrets onto the board.

Board files are independent copies, not a view of the repo, so editing a module
here changes nothing until it is copied across. That has produced two silent
failures already: a stale `secrets.py` missing SENDER_HOST, and a stale
`protocol.py` missing zero(), the latter surfacing only as a button that
appeared to do nothing.

Copies only what changed, by comparing hashes, so running it constantly is
cheap.

    python tools/sync_board.py
    python tools/sync_board.py --device id:XXXX --force

The receiver is the other board, and a different shape of job: one file, no
dependencies, and installed as main.py because it exists to come up on its own
when the shop PC does.

    python tools/sync_board.py --receiver

That defaults to the receiver board rather than the configured one, and ignores
PICO_DEVICE. Both boards are ESP32-S3 and enumerate identically, so a mode that
inherited the pendant's device would quietly replace the pendant's entry point
with the receiver's - and the symptom is a pendant that no longer does anything,
some time later, with nothing to connect it to this command.
"""

import argparse
import hashlib
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import board  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
PENDANT = ROOT / "micropython" / "pendant"
RECEIVER = ROOT / "micropython" / "receiver" / "receiver.py"

# Modules the pendant imports at runtime. Self-tests and probes are not copied:
# `mpremote run` streams them from here, so they are always current.
# Anything a board-side script imports has to be here. `mpremote run` streams
# the script being run but not its imports, so a module missing from this list
# fails as "no module named 'pendant'" - the second half of the try/except
# import - which reads like a packaging problem rather than a missing file.
MODULES = ["quadrature.py", "quadrature_pcnt.py", "protocol.py", "link.py",
           "espnow_link.py", "jog.py", "buttons.py", "ili9341.py",
           "st7796.py", "tca9554.py", "screen.py", "touch.py"]
EXTRA = [ROOT / "micropython" / "secrets.py"]

DEFAULT_DEVICE = board.device()


def mpremote(device, *args):
    result = subprocess.run(
        [sys.executable, "-m", "mpremote", "connect", device, *args],
        capture_output=True, text=True)
    return result.returncode, (result.stdout + result.stderr).strip()


def board_hashes(device):
    """Hash every file on the board in one round trip.

    One exec rather than a stat per file: each mpremote invocation reopens the
    serial port and costs the best part of a second, which would make syncing
    slower than just copying everything blindly.
    """
    script = (
        "import hashlib,binascii,os\n"
        "for n in os.listdir('/'):\n"
        "    if n.endswith('.py'):\n"
        "        h=hashlib.sha256()\n"
        "        f=open(n,'rb')\n"
        "        while True:\n"
        "            b=f.read(512)\n"
        "            if not b: break\n"
        "            h.update(b)\n"
        "        f.close()\n"
        "        print(n, binascii.hexlify(h.digest()).decode())\n"
    )
    code, output = mpremote(device, "exec", script)
    if code != 0:
        return None
    hashes = {}
    for line in output.splitlines():
        parts = line.split()
        if len(parts) == 2:
            hashes[parts[0]] = parts[1]
    return hashes


def local_hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def install_main(device, source, remote):
    """Copy one file to the board as main.py, if it is not already there.

    Shared by both boards because the awkward part is the same for each: the
    source and the destination have different names, so it cannot be compared
    under its own name the way every other file can.
    """
    if remote is not None and remote.get("main.py") == local_hash(source):
        print("  main.py already current")
        return True, 0

    code, output = mpremote(device, "fs", "cp", str(source), ":main.py")
    if code != 0:
        print("  FAILED main.py: {}".format(output))
        return False, 0
    print("  installed {} as main.py".format(source.name))
    return True, 1


def sync_receiver(device, force):
    """Install the receiver: one file, as main.py, and nothing else.

    Nothing like the pendant's module list, because receiver.py imports only
    what the ESP32 port already has - sys, select, time, network, espnow. No
    secrets.py either: ESP-NOW needs no credentials, which is a good part of why
    the pendant is moving to it.

    It goes on as main.py with no opt-in, unlike the pendant's --main. There is
    no development mode to protect here: this board lives plugged into the shop
    PC with nobody to launch anything, and `on_board.py --no-sync` streams the
    file when you want to watch it run without installing it.
    """
    if not RECEIVER.exists():
        print("no receiver at {}".format(RECEIVER))
        return 1

    # Every ESP32-S3 here enumerates identically, so nothing downstream of this
    # point can tell which board it is talking to. Refused rather than warned
    # about: there is no reason to put the receiver's firmware on the pendant,
    # and the failure is silent and delayed - the pendant comes up as a
    # receiver, does nothing, and looks broken rather than misflashed.
    #
    # The rule is the name, not the identity, so a second or third receiver
    # needs no edit here: board.py names them receiver, receiver2 and so on.
    # A board with no name at all is allowed through, because a receiver being
    # set up for the first time has not been named yet.
    names = sorted(n for n, d in board.BOARDS.items() if d == device)
    if names and not any(n.startswith(board.RECEIVER_PREFIX) for n in names):
        print("{} is '{}' in board.py, not a receiver."
              .format(device, "/".join(names)))
        print("  Installing receiver.py as main.py there would replace that")
        print("  board's entry point. Names beginning 'receiver' are the ones")
        print("  this accepts; rename it there if that is really the intent.")
        return 1

    remote = None if force else board_hashes(device)
    if remote is None and not force:
        print("could not read the board - is it connected and free?")
        print("  the sender, espnow_bridge.py and mpremote all want the port")
        print("  exclusively, so only one of them can hold it at a time.")
        return 1

    ok, copied = install_main(device, RECEIVER, remote)
    if not ok:
        return 1

    print("\n{} copied, {} already current".format(copied, 1 - copied))
    if copied:
        # mpremote leaves the board in the REPL, so main.py is installed but not
        # running. Unplugging it to move it to the shop PC resolves that on its
        # own, which is exactly why it is worth saying here - set one up in
        # place, look at the port, see nothing, and the obvious conclusion is
        # that the install failed.
        print("\nreset or replug the board to start it - it is installed but")
        print("still sitting in the REPL until then.")
        print("\nit will run on power-up after that, with nothing to launch.")
        print("point the sender's pendant serial port at it and restart the")
        print("sender - espnow_bridge.py is retired and must not still hold")
        print("the port.")
    return 0


def sync_pendant(device, force, install_entry):
    files = [PENDANT / name for name in MODULES] + EXTRA
    missing = [f for f in files if not f.exists()]
    for f in missing:
        print("  skipping {} (not found)".format(f.name))
    files = [f for f in files if f.exists()]

    remote = None if force else board_hashes(device)
    if remote is None and not force:
        print("could not read the board - is it connected and free?")
        return 1

    copied = skipped = 0
    for path in files:
        if remote is not None and remote.get(path.name) == local_hash(path):
            skipped += 1
            continue
        code, output = mpremote(device, "fs", "cp",
                                str(path), ":" + path.name)
        if code != 0:
            print("  FAILED {}: {}".format(path.name, output))
            return 1
        print("  copied {}".format(path.name))
        copied += 1

    # main.py is deliberately not part of MODULES.
    #
    # Copying it every sync would make every development run also change what
    # the board does when it is next switched on, which is a different decision
    # and should be a deliberate one. It also cannot be hash-compared under its
    # own name, since the source is pendant.py and the destination is main.py.
    if install_entry:
        ok, wrote = install_main(device, PENDANT / "pendant.py", remote)
        if not ok:
            return 1
        copied += wrote
        skipped += 1 - wrote

    print("\n{} copied, {} already current".format(copied, skipped))
    if copied:
        print("restart the pendant for the new modules to take effect")
    if install_entry:
        print("\nthe board will now run the pendant on power-up, with no PC.")
        print("mpremote still interrupts it, so run_pendant.py works as before")
        print("- and that is the only way to see the one-liner, since output")
        print("goes nowhere when no host is attached.")
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device", default=None,
                        help="mpremote target, e.g. id:XXXX or COM5")
    parser.add_argument("--force", action="store_true",
                        help="copy everything, skipping the hash comparison")
    parser.add_argument("--main", action="store_true",
                        help="also install pendant.py as main.py, so the "
                             "board runs the pendant on power-up")
    parser.add_argument("--receiver", action="store_true",
                        help="install the receiver instead: receiver.py "
                             "becomes main.py on the receiver board, and "
                             "nothing else is copied")
    args = parser.parse_args()

    if args.receiver and args.main:
        print("--main installs the pendant's entry point and --receiver")
        print("installs the receiver's. They write different files to the")
        print("same main.py, so pick one.")
        return 1

    # Resolved after parsing rather than at import, so --receiver can change
    # what "no --device" means. Passing the name explicitly also beats
    # PICO_DEVICE inside board.device(), which is what stops a session pointed
    # at the pendant from sending the receiver there.
    if args.receiver:
        return sync_receiver(board.device(args.device or "receiver"),
                             args.force)

    return sync_pendant(board.device(args.device), args.force, args.main)


raise SystemExit(main())
