"""Sync the board and start the pendant, in one command.

    python tools/run_pendant.py

Exists because the two-step version is easy to get wrong in both directions:
forgetting the run command looks like the pendant is broken when it simply was
not started, and forgetting the sync silently runs stale modules. Both have
happened. Doing them together removes the choice.

Ctrl-C stops the pendant. Note that this detaches mpremote; the script itself
keeps running on the board until the next connection interrupts it.

    python tools/run_pendant.py --no-sync     skip the sync
    python tools/run_pendant.py --device COM5
"""

import argparse
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import board  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
ENTRY = ROOT / "micropython" / "pendant" / "pendant.py"
DEFAULT_DEVICE = board.device()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device", default=DEFAULT_DEVICE)
    parser.add_argument("--no-sync", action="store_true")
    args = parser.parse_args()

    if not args.no_sync:
        print("syncing board...")
        sync = subprocess.run(
            [sys.executable, str(ROOT / "tools" / "sync_board.py"),
             "--device", args.device])
        if sync.returncode != 0:
            print("\nsync failed - is the board connected and the port free?")
            print("a pendant already running holds the port; Ctrl-C it first.")
            return sync.returncode

    print("\nstarting pendant (Ctrl-C to stop)...\n")
    try:
        return subprocess.run(
            [sys.executable, "-m", "mpremote", "connect", args.device,
             "run", str(ENTRY)]).returncode
    except KeyboardInterrupt:
        print("\nstopped.")
        return 0


raise SystemExit(main())
