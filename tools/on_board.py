"""Run a script on the board without typing a device ID.

Wraps `mpremote run` so the self-tests and probes have one documented way to be
launched. The point is that no one has to remember, or copy from a docstring,
the serial number of the board - which is how `connect auto` gets reached for,
and `connect auto` on this bench meant the grblHAL controller.

    python tools/on_board.py micropython/pendant/selftest_link.py
    python tools/on_board.py micropython/pendant/monitor_encoder.py

Output streams live, so anything interactive works as it would over `mpremote
run` directly. Ctrl-C stops it.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import board  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("script", help="path to the script, relative to the repo")
    parser.add_argument("--device", default=None,
                        help="override the board (default: PICO_DEVICE, then "
                             "the configured board)")
    args = parser.parse_args()

    path = Path(args.script)
    if not path.exists():
        print("no such script: {}".format(path))
        return 1

    device = board.device(args.device)
    print("running {} on {}".format(path, device))
    print("-" * 46)
    # Not captured: these scripts are watched while they run, and buffering
    # their output until exit would make a live probe useless.
    code, _ = board.mpremote(device, "run", str(path), capture=False)
    return code


if __name__ == "__main__":
    sys.exit(main())
