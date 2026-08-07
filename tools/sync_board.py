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
"""

import argparse
import hashlib
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PENDANT = ROOT / "micropython" / "pendant"

# Modules the pendant imports at runtime. Self-tests and probes are not copied:
# `mpremote run` streams them from here, so they are always current.
MODULES = ["quadrature.py", "protocol.py", "link.py", "jog.py", "buttons.py"]
EXTRA = [ROOT / "micropython" / "secrets.py"]

DEFAULT_DEVICE = "id:7BE7DD09548134C0"


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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device", default=DEFAULT_DEVICE,
                        help="mpremote target, e.g. id:XXXX or COM5")
    parser.add_argument("--force", action="store_true",
                        help="copy everything, skipping the hash comparison")
    args = parser.parse_args()

    files = [PENDANT / name for name in MODULES] + EXTRA
    missing = [f for f in files if not f.exists()]
    for f in missing:
        print("  skipping {} (not found)".format(f.name))
    files = [f for f in files if f.exists()]

    remote = None if args.force else board_hashes(args.device)
    if remote is None and not args.force:
        print("could not read the board - is it connected and free?")
        return 1

    copied = skipped = 0
    for path in files:
        if remote is not None and remote.get(path.name) == local_hash(path):
            skipped += 1
            continue
        code, output = mpremote(args.device, "fs", "cp",
                                str(path), ":" + path.name)
        if code != 0:
            print("  FAILED {}: {}".format(path.name, output))
            return 1
        print("  copied {}".format(path.name))
        copied += 1

    print("\n{} copied, {} already current".format(copied, skipped))
    if copied:
        print("restart the pendant for the new modules to take effect")
    return 0


raise SystemExit(main())
