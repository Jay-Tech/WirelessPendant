"""Take a board from blank to working, in one command.

    python tools/setup.py

Setting a pendant up used to be about a dozen steps spread across two tools,
and two of those steps were editing a source file with data:

  - `tools/board.py` holds the bench's USB serial numbers, and every one of
    them is wrong on anyone else's bench. It had to be edited before any other
    tool would find the board at all.
  - `micropython/secrets.py` had to be copied from its template and filled in
    by hand, including which transport to use - a Python constant standing in
    for what is really a question with two answers.

Around those: the BOOT/RESET dance, finding the port, erase, write at offset 0
rather than the 0x1000 an original ESP32 takes, reset, discover that Windows
has renumbered the port, go back and find the new serial number. Then most of
it again for the receiver, then type the receiver's port into the sender.

This asks instead. It finds the board, flashes it if it needs flashing,
confirms the hardware matches the role, collects the settings, installs, and
then proves it worked by reading what the board says on the way up.

    python tools/setup.py --pendant
    python tools/setup.py --receiver
    python tools/setup.py --list

Safety, which is the part worth reading:

Every tool here targets an explicit device because `mpremote connect auto` once
grabbed the STM32 grblHAL controller on this bench and sent it raw-REPL control
bytes with a machine powered. Automatic detection has to be *more* careful than
the thing it replaces, not less. So nothing is opened unless its USB descriptor
is on the allowlist in `board.TALKABLE` - two MicroPython descriptors, and
nothing else. An unrecognised device is listed and skipped, never probed, and
the controller is called out by name so it is visible that it was seen and left
alone.
"""

import argparse
import getpass
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import board  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
PENDANT_DIR = ROOT / "micropython" / "pendant"
SECRETS = ROOT / "micropython" / "secrets.py"

# The AXP2101 power-management chip, which the pendant carries and a bare
# receiver board does not. Address, register and expected value all come from
# `pendant/axp2101.py`; the pins from `pendant/probe_axp2101.py`.
#
# The chip ID is checked rather than the address being pinged, and that
# distinction is `axp2101.present()`'s, not mine: something else acknowledging
# 0x34 on a board that is not this one would otherwise read as a pendant.
AXP_ADDRESS = 0x34
AXP_CHIP_ID_REG = 0x03
AXP_CHIP_ID = 0x4A
AXP_SCL, AXP_SDA = 7, 8

DEFAULT_SENDER_PORT = 8422

# How long to wait for a board to appear in a given USB state. Generous: the
# operator is being asked to press two buttons in a particular order, and a
# timeout that expires while they find the board is a failure report for
# something that was going fine.
APPEAR_TIMEOUT_S = 120
REAPPEAR_TIMEOUT_S = 60


# --- talking to the operator ---------------------------------------------

def say(message=""):
    print(message)


def ask(prompt, default=None):
    suffix = " [{}]".format(default) if default else ""
    while True:
        answer = input("{}{}: ".format(prompt, suffix)).strip()
        if answer:
            return answer
        if default is not None:
            return default


def confirm(prompt, default=False):
    hint = "Y/n" if default else "y/N"
    answer = input("{} [{}]: ".format(prompt, hint)).strip().lower()
    if not answer:
        return default
    return answer.startswith("y")


def choose(prompt, options):
    """Pick one of a numbered list. Returns the chosen item."""
    for index, (label, _) in enumerate(options, 1):
        say("  {}) {}".format(index, label))
    while True:
        answer = input("{} [1-{}]: ".format(prompt, len(options))).strip()
        if answer.isdigit() and 1 <= int(answer) <= len(options):
            return options[int(answer) - 1][1]


# --- the device listing ---------------------------------------------------

def report_attached(devices):
    """Show everything attached, including what is deliberately not touched."""
    if not devices:
        say("no serial devices found, or mpremote is not installed:")
        say("  python -m pip install --upgrade mpremote")
        return

    say("attached:")
    for dev in devices:
        if dev["vidpid"] == board.CONTROLLER:
            note = "<- grblHAL CONTROLLER, not touched"
        elif dev["vidpid"] in board.TALKABLE:
            note = "<- {}".format(board.KNOWN[dev["vidpid"]])
        elif dev["vidpid"] == board.ROM_BOOTLOADER:
            note = "<- ESP32-S3 in ROM bootloader, ready to flash"
        else:
            note = ""
        say("  {:<7} {:<18} {:<11} {}".format(
            dev["port"], dev["serial"] or "-", dev["vidpid"], note))


def describe_roles(devices):
    """Ask each safe board what it is. One REPL round trip per board.

    Deliberately not part of `board.py`'s own listing: opening a REPL soft
    interrupts whatever the board is running, and the tool people run just to
    look at what is plugged in should not stop a receiver receiving.
    """
    roles = {}
    for dev in devices:
        if dev["vidpid"] not in board.TALKABLE or not dev["serial"]:
            continue
        marker = board.read_marker("id:" + dev["serial"])
        roles[dev["serial"]] = marker.get("role") if marker else None
    return roles


# --- esptool --------------------------------------------------------------

def esptool_command(name):
    """Subcommand spelling for the installed esptool.

    esptool 5 renamed `write_flash` to `write-flash` and warns on the old form;
    esptool 4 only knows the old one. Both are current in the wild, so this
    picks rather than pinning a version - a deprecation warning in the middle
    of a flash reads like a fault to someone setting a board up for the first
    time.
    """
    result = subprocess.run([sys.executable, "-m", "esptool", "version"],
                            capture_output=True, text=True)
    match = re.search(r"(\d+)\.\d+", result.stdout)
    major = int(match.group(1)) if match else 4
    return name.replace("_", "-") if major >= 5 else name


# The download page offers several ESP32-S3 builds, and the boards here want
# this one. Both the pendant and the receiver report "Octal-SPIRAM" from
# os.uname().machine, and the plain build leaves that memory unused - which
# does not fail at the flash, or at the boot, but later and elsewhere: the
# pendant paints a 320x480 display out of framebuffers, and the allocation that
# runs out reads as a screen bug rather than a wrong build.
PREFERRED_VARIANT = "SPIRAM_OCT"


def resolve_firmware(explicit):
    """Which .bin to flash. Asks when it genuinely cannot tell.

    An earlier version refused outright when the repo root held more than one
    build, which is the wrong instinct for a tool that asks about everything
    else - and it hits at exactly the moment a version bump leaves the old file
    beside the new one, so it greeted people mid-upgrade with a dead end.

    It now picks when there is a right answer and asks when there is not, which
    is the same rule as everywhere else here.
    """
    if explicit:
        # A bare filename is resolved against the repo root, because that is
        # where the builds live and where the message below lists them from -
        # so what someone copies out of that list works from any directory.
        path = Path(explicit)
        if not path.exists():
            path = ROOT / explicit
        if not path.exists():
            say("no such firmware: {}".format(explicit))
            say("  builds at the repo root:")
            for found in sorted(ROOT.glob("*.bin")):
                say("    {}".format(found.name))
            return None
        return path

    candidates = sorted(ROOT.glob("ESP32_GENERIC_S3*.bin"))
    if not candidates:
        say("no ESP32-S3 MicroPython build at {}".format(ROOT))
        say("  download one from micropython.org/download/ESP32_GENERIC_S3/")
        say("  and put the .bin at the repo root.")
        return None

    if len(candidates) == 1:
        return candidates[0]

    preferred = [p for p in candidates if PREFERRED_VARIANT in p.name]
    if len(preferred) == 1:
        say("more than one ESP32-S3 build at the repo root; using the one")
        say("these boards want:")
        say("  {}".format(preferred[0].name))
        say("")
        say("The others carry no octal SPIRAM, which this hardware has. To")
        say("override:  python tools/setup.py --firmware <name>")
        say("")
        return preferred[0]

    # Several of the preferred variant, or none of them - usually two versions
    # sitting side by side after an upgrade. Nothing here can tell which is
    # wanted, so ask rather than guess, newest name last.
    say("more than one ESP32-S3 build at the repo root, and no way to tell")
    say("which you want:")
    say("")
    options = [(p.name, p) for p in (preferred or candidates)]
    return choose("firmware", options)


def wait_for(predicate, timeout, message):
    """Poll the device listing until something matches. Returns it, or None."""
    say(message)
    deadline = time.time() + timeout
    dots = 0
    while time.time() < deadline:
        for dev in board.attached():
            if predicate(dev):
                say("")
                return dev
        time.sleep(1)
        dots += 1
        if dots % 5 == 0:
            print("  still waiting ({}s)".format(int(time.time() - deadline + timeout)))
    say("")
    return None


def flash(firmware, erase=True):
    """Put MicroPython on an ESP32-S3, from the ROM bootloader.

    The button sequence cannot be automated - these boards use the S3's native
    USB, where esptool's DTR/RTS auto-reset does not reliably reach the ROM
    bootloader. What can be automated is the waiting: rather than telling the
    operator to press buttons and then go and find the port themselves, this
    watches for the bootloader descriptor to appear and carries on by itself.
    That removes the two steps this actually goes wrong at - getting the button
    order wrong, and looking up a port that has since been renumbered.
    """
    say("Put the board into its ROM bootloader:")
    say("  hold BOOT, tap RESET, release BOOT.")
    say("")
    target = wait_for(lambda d: d["vidpid"] == board.ROM_BOOTLOADER,
                      APPEAR_TIMEOUT_S,
                      "waiting for the bootloader to appear...")
    if target is None:
        say("no board appeared in the ROM bootloader within {}s."
            .format(APPEAR_TIMEOUT_S))
        say("  It enumerates as {} when it is there; `python tools/board.py`"
            .format(board.ROM_BOOTLOADER))
        say("  lists what is attached. In MicroPython it is {} instead, and"
            .format(board.MICROPYTHON_S3))
        say("  esptool will not talk to that one.")
        return False

    port = target["port"]
    say("bootloader on {}".format(port))

    known = {d["serial"] for d in board.attached()
             if d["vidpid"] == board.MICROPYTHON_S3 and d["serial"]}

    if erase:
        say("erasing flash...")
        code = subprocess.run(
            [sys.executable, "-m", "esptool", "--chip", "esp32s3",
             "--port", port, esptool_command("erase_flash")]).returncode
        if code != 0:
            say("erase failed.")
            return False

    say("writing {}...".format(firmware.name))
    # Offset 0, not the 0x1000 an original ESP32 takes. Writing an S3 image at
    # 0x1000 produces a board that enumerates and never boots.
    code = subprocess.run(
        [sys.executable, "-m", "esptool", "--chip", "esp32s3",
         "--port", port, "--baud", "460800",
         esptool_command("write_flash"), "-z", "0", str(firmware)]).returncode
    if code != 0:
        say("write failed.")
        return False

    say("")
    say("Tap RESET to leave the bootloader.")
    target = wait_for(
        lambda d: (d["vidpid"] == board.MICROPYTHON_S3
                   and d["serial"] and d["serial"] not in known),
        REAPPEAR_TIMEOUT_S,
        "waiting for MicroPython to come up...")

    if target is None:
        # The board keeps its serial across a reflash, so "new" finds nothing
        # when an already-set-up board is being redone. Falling back to "the
        # only one there" is safe; anything more ambiguous is handed back.
        s3s = [d for d in board.attached()
               if d["vidpid"] == board.MICROPYTHON_S3 and d["serial"]]
        if len(s3s) == 1:
            target = s3s[0]
        else:
            say("the board did not come back as MicroPython, or more than one")
            say("candidate is attached. Re-run with --device id:XXXX once")
            say("`python tools/board.py` shows which it is.")
            return False

    say("MicroPython up on {}, id:{}".format(target["port"], target["serial"]))
    return "id:" + target["serial"]


# --- what the board actually is ------------------------------------------

def micropython_version(dev):
    code, output = board.mpremote(
        dev, "exec", "import os\nprint(os.uname().release, os.uname().machine)\n")
    return output.strip() if code == 0 else None


def has_pendant_hardware(dev):
    """Is the AXP2101 on this board? True, False, or None if it could not tell.

    The pendant has a power-management chip and a bare receiver board does not,
    which is the only way to tell two identical-enumerating ESP32-S3s apart
    before either has been given a role. None is returned rather than False
    when the bus itself fails, because "no answer" and "wrong board" call for
    different things to be said.
    """
    script = (
        "try:\n"
        "    from machine import I2C, Pin\n"
        "    bus = I2C(0, scl=Pin({scl}), sda=Pin({sda}), freq=400000)\n"
        "    found = bus.scan()\n"
        "    if {addr} in found:\n"
        "        print('chip', bus.readfrom_mem({addr}, {reg}, 1)[0])\n"
        "    else:\n"
        "        print('absent')\n"
        "except Exception as e:\n"
        "    print('error', e)\n"
    ).format(scl=AXP_SCL, sda=AXP_SDA, addr=AXP_ADDRESS, reg=AXP_CHIP_ID_REG)

    code, output = board.mpremote(dev, "exec", script)
    if code != 0:
        return None
    text = output.strip()
    if text.startswith("chip"):
        try:
            return int(text.split()[1]) == AXP_CHIP_ID
        except (IndexError, ValueError):
            return None
    if text.startswith("absent"):
        return False
    return None


def check_role_against_hardware(dev, role):
    """Refuse the two mismatches that are silent at the time and costly later."""
    present = has_pendant_hardware(dev)

    if present is None:
        say("could not read the I2C bus to confirm what this board is.")
        return confirm("continue anyway?", default=False)

    if role == board.PENDANT_ROLE and not present:
        say("this board has no AXP2101, so it does not look like pendant")
        say("hardware - it is more likely the receiver. Installing the")
        say("pendant here would leave the real pendant untouched and this")
        say("board doing nothing, which looks like a broken pendant later.")
        return confirm("install the pendant on it anyway?", default=False)

    if role == board.RECEIVER_ROLE and present:
        say("this board HAS an AXP2101, so it is pendant hardware. Installing")
        say("the receiver would replace the pendant's main.py - the pendant")
        say("would come up as a receiver, do nothing, and read as misflashed")
        say("rather than misconfigured, some time later.")
        return confirm("really install the receiver on the pendant?",
                       default=False)

    return True


# --- settings -------------------------------------------------------------

def scan_networks(dev):
    """Ask the board what WiFi it can see, strongest first.

    From the board rather than from this PC deliberately: the pendant's radio
    is what has to reach the access point, and a network the laptop can see
    from the office is not evidence about a board in the shop.
    """
    script = (
        "import network\n"
        "w = network.WLAN(network.STA_IF)\n"
        "w.active(True)\n"
        "seen = []\n"
        "try:\n"
        "    for n in w.scan():\n"
        "        try:\n"
        "            name = n[0].decode()\n"
        "        except Exception:\n"
        "            continue\n"
        "        if name and (name, n[3]) not in seen:\n"
        "            seen.append((name, n[3]))\n"
        "except Exception as e:\n"
        "    print('error', e)\n"
        "for name, rssi in sorted(seen, key=lambda s: -s[1]):\n"
        "    print('net', rssi, name)\n"
    )
    code, output = board.mpremote(dev, "exec", script)
    if code != 0:
        return []
    networks = []
    for line in output.splitlines():
        parts = line.split(None, 2)
        if len(parts) == 3 and parts[0] == "net":
            networks.append((parts[2], parts[1]))
    return networks


def collect_settings(dev):
    """Everything that used to be a hand edit of secrets.py."""
    say()
    say("Which transport should the pendant use?")
    transport = choose("transport", [
        ("ESP-NOW - talks straight to the receiver board at the sender's PC. "
         "No WiFi, no credentials, nothing to configure.", True),
        ("WiFi - joins your network and opens a TCP session to the sender.",
         False),
    ])

    settings = {"USE_ESPNOW": transport, "SENDER_PORT": DEFAULT_SENDER_PORT}

    if transport:
        # ESP-NOW needs none of the rest, but secrets.py is imported whole and
        # pendant.py reads SENDER_HOST through getattr with a default. Writing
        # the placeholders keeps the file switchable back to WiFi by editing
        # one line, which is what the transport constant was for.
        settings["WIFI_SSID"] = ""
        settings["WIFI_PASSWORD"] = ""
        settings["HOSTNAME"] = "pendant"
        settings["SENDER_HOST"] = ""
        say()
        say("ESP-NOW selected - no network settings needed.")
        say("Set the receiver up too, if you have not:")
        say("  python tools/setup.py --receiver")
        return settings

    say()
    say("scanning for networks from the board...")
    networks = scan_networks(dev)
    if networks:
        options = [("{}  ({} dBm)".format(name, rssi), name)
                   for name, rssi in networks[:12]]
        options.append(("enter a name by hand (hidden network)", None))
        say()
        ssid = choose("network", options)
        if ssid is None:
            ssid = ask("SSID")
    else:
        say("the board saw no networks - entering the name by hand.")
        ssid = ask("SSID")

    # getpass so it is not echoed to a terminal someone may be sharing, and so
    # it does not sit in the scrollback of a shop PC. It is written to
    # secrets.py, which is gitignored, and to the board.
    password = getpass.getpass("password for {}: ".format(ssid))

    settings["WIFI_SSID"] = ssid
    settings["WIFI_PASSWORD"] = password
    settings["HOSTNAME"] = ask("hostname the pendant announces", "pendant")
    say()
    say("The machine running the sender, on that network. Its absence here")
    say("once cost an evening: the pendant joins, then fails in a way that")
    say("reads as a network fault rather than a missing setting.")
    settings["SENDER_HOST"] = ask("sender address")
    settings["SENDER_PORT"] = int(ask("sender port", str(DEFAULT_SENDER_PORT)))
    return settings


def write_secrets(settings):
    """Generate secrets.py. Returns the path.

    Written to the repo as well as copied to the board, because sync_board.py
    copies it from there on every later sync - so a board configured here and
    synced tomorrow would otherwise be handed whatever secrets.py said before.
    """
    lines = [
        '"""WiFi and transport settings, written by tools/setup.py.',
        "",
        "Gitignored. Edit and re-run `python tools/sync_board.py` to change a",
        "board that is already set up, or run setup.py again.",
        '"""',
        "",
        "WIFI_SSID = {!r}".format(settings["WIFI_SSID"]),
        "WIFI_PASSWORD = {!r}".format(settings["WIFI_PASSWORD"]),
        "HOSTNAME = {!r}".format(settings["HOSTNAME"]),
        "",
        "SENDER_HOST = {!r}".format(settings["SENDER_HOST"]),
        "SENDER_PORT = {!r}".format(settings["SENDER_PORT"]),
        "",
        "# True talks ESP-NOW to the receiver board at the sender's PC and",
        "# ignores everything above. False joins the WiFi and opens a TCP",
        "# session to SENDER_HOST.",
        "USE_ESPNOW = {!r}".format(settings["USE_ESPNOW"]),
        "",
    ]
    SECRETS.write_text("\n".join(lines), encoding="utf-8")
    return SECRETS


# --- installing -----------------------------------------------------------

def sync(device, *flags):
    """Hand the copying to sync_board.py rather than repeating it.

    That tool already knows which modules the pendant imports, that copies are
    hash-compared to stay cheap, and that main.py cannot be compared under its
    own name because the source is called something else. All three have cost a
    silent failure here before; a second implementation would get to repeat
    them.
    """
    script = Path(__file__).resolve().parent / "sync_board.py"
    return subprocess.run(
        [sys.executable, str(script), "--device", device, *flags]).returncode


def read_boot_output(device, seconds=8):
    """Reset the board and read what it says on the way up.

    A copy returning success proves a file landed, not that the board runs -
    which is the difference between a working pendant and one whose modules are
    all current beside an entry point that faults on import. So this restarts it
    and reads back, and the caller reports what came out.
    """
    try:
        import serial
    except ImportError:
        return None

    port = board.port(device)
    if port is None:
        return None

    board.mpremote(device, "reset")

    # The S3's USB is on the chip, so a reset drops the port and re-enumerates
    # it. Opening too early raises rather than blocking.
    deadline = time.time() + 15
    handle = None
    while time.time() < deadline and handle is None:
        time.sleep(1)
        port = board.port(device) or port
        try:
            handle = serial.Serial(port, 115200, timeout=1)
        except Exception:
            handle = None

    if handle is None:
        return None

    lines = []
    try:
        end = time.time() + seconds
        while time.time() < end:
            raw = handle.readline()
            if not raw:
                continue
            text = raw.decode("utf-8", "replace").strip()
            if text:
                lines.append(text)
    finally:
        handle.close()
    return lines


# --- the sender's own configuration ---------------------------------------

def sender_config_path():
    """Where the C# sender keeps its settings, on this machine.

    Matches ConfigManager: SpecialFolder.ApplicationData, which is %AppData% on
    Windows and ~/.config on the Pi.
    """
    if os.name == "nt":
        base = os.environ.get("APPDATA")
        if not base:
            return None
        root = Path(base)
    else:
        root = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return root / "GrblHAL-Sender" / "Config" / "GHalSender_Config.json"


def offer_sender_config(port):
    """Point the sender at the receiver we just set up, if asked.

    The sender deliberately never scans for this port - `PendantConfig` says so
    and gives the reason: a controller and a receiver both enumerate as
    anonymous USB serial, so a sender guessing could open the controller. That
    reasoning is about guessing. This is not guessing; it just flashed the board
    and knows the port. The rule stays as it is.
    """
    path = sender_config_path()
    if path is None or not path.exists():
        say()
        say("The sender's config is not on this machine yet, so set the port")
        say("in the app instead:")
        say("  Pendant -> serial port: {}".format(port))
        say("  Pendant -> enabled: yes   (it needs a restart to take effect)")
        return

    say()
    say("Found the sender's config at:")
    say("  {}".format(path))
    if not confirm("point it at {} and enable the pendant?".format(port),
                   default=True):
        return

    try:
        config = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception as error:
        say("could not read it ({}) - set the port in the app instead."
            .format(error))
        return

    # Patched in place, never created. The sender deserialises the whole object,
    # so a file written from scratch here would come back with defaults for
    # every setting it does not know about - which would silently reset the
    # machine's configuration, and this is not the tool to do that from.
    pendant = config.get("PendantConfig")
    if not isinstance(pendant, dict):
        say("no PendantConfig section - run the sender once, then re-run this.")
        return

    pendant["SerialPortName"] = port
    pendant["Enabled"] = True

    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(config, indent=2), encoding="utf-8")
    temp.replace(path)
    say("done. Restart the sender - the pendant listener starts at launch.")
    say("Only one thing may hold the port, so the sender and mpremote cannot")
    say("both have it: close the sender before running these tools again.")


# --- the flow -------------------------------------------------------------

def pick_target(role, explicit, args, allow_flash):
    """Find the board to work on, flashing a blank one if that is what is there.

    Which firmware to flash is resolved here rather than up front, because that
    is the only branch that needs it - asking someone to choose a build before
    finding out their board does not need flashing is a question about nothing.
    """
    if explicit:
        return board.device(explicit)

    devices = board.attached()
    report_attached(devices)
    say()

    roles = describe_roles(devices)
    already = [s for s, r in roles.items() if r == role]
    if len(already) == 1:
        dev = "id:" + already[0]
        say("id:{} already says it is the {}.".format(already[0], role))
        if confirm("update it?", default=True):
            return dev
    elif len(already) > 1:
        say("more than one board says it is the {}:".format(role))
        for serial in already:
            say("  id:{}".format(serial))
        say("pass --device id:XXXX to say which.")
        return None

    candidates = [d for d in devices if d["vidpid"] in board.TALKABLE
                  and d["serial"]]
    unclaimed = [d for d in candidates if not roles.get(d["serial"])]

    if unclaimed:
        say("boards with no role yet:")
        options = [("{}  id:{}".format(d["port"], d["serial"]),
                    "id:" + d["serial"]) for d in unclaimed]
        if allow_flash:
            options.append(("none of these - flash a new board", None))
        chosen = choose("use", options) if len(options) > 1 else options[0][1]
        if chosen:
            return chosen

    if not allow_flash:
        say("no board to use, and --no-flash was given.")
        return None

    say()
    if not confirm("flash a blank board now?", default=True):
        return None

    firmware = resolve_firmware(args.firmware)
    if firmware is None:
        return None
    return flash(firmware)


def run(role, args):
    say("Setting up the {}.".format(role))
    say()

    device = pick_target(role, args.device, args, not args.no_flash)
    if not device:
        return 1

    version = micropython_version(device)
    if version:
        say("board reports MicroPython {}".format(version))
    else:
        say("the board did not answer. It has to be in MicroPython, free, and")
        say("not held by the sender or another mpremote - the port is")
        say("exclusive, so only one of them can have it.")
        return 1

    say()
    if not check_role_against_hardware(device, role):
        say("stopped.")
        return 1

    if role == board.PENDANT_ROLE:
        settings = collect_settings(device)
        path = write_secrets(settings)
        say()
        say("wrote {}".format(path))

    # Written before the install so sync_board's receiver guard can see the role
    # the operator just confirmed. If the install then fails the board is marked
    # but not set up, which the next run corrects.
    say()
    board.write_marker(device, role, {"tool": "setup.py"})

    say("installing...")
    flags = ["--receiver"] if role == board.RECEIVER_ROLE else ["--main"]
    if sync(device, *flags) != 0:
        say()
        say("install failed. The board is marked as the {} but does not have"
            .format(role))
        say("its firmware yet - re-run this once the cause is cleared.")
        return 1

    say()
    say("restarting the board to check it comes up...")
    lines = read_boot_output(device)
    if lines is None:
        say("could not read the port back - reset the board and watch it with:")
        say("  python -m mpremote connect {} repl".format(device))
    elif not lines:
        say("nothing came back within the window. That is not proof of a")
        say("fault - a pendant on battery with no host prints nowhere - but")
        say("it is not proof it works either. Watch it with:")
        say("  python -m mpremote connect {} repl".format(device))
    else:
        say("the board says:")
        for line in lines[:12]:
            say("  {}".format(line))

    say()
    say("{} set up as {}.".format(device, role))

    if role == board.RECEIVER_ROLE:
        port = board.port(device)
        if port:
            offer_sender_config(port)
    return 0


def main():
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--pendant", action="store_true",
                        help="set up the handheld")
    parser.add_argument("--receiver", action="store_true",
                        help="set up the ESP-NOW receiver at the sender's PC")
    parser.add_argument("--list", action="store_true",
                        help="show what is attached and what each board says "
                             "it is, and do nothing else")
    parser.add_argument("--device", default=None,
                        help="skip detection and use this board, e.g. id:XXXX")
    parser.add_argument("--firmware", metavar="FILE.bin", default=None,
                        help="MicroPython build to flash, e.g. --firmware "
                             "ESP32_GENERIC_S3-SPIRAM_OCT-20260824-v1.29.0.bin "
                             "- a bare name is looked for at the repo root. "
                             "Default: the build there, preferring the "
                             "SPIRAM_OCT one these boards need")
    parser.add_argument("--no-flash", action="store_true",
                        help="never flash; fail instead if the board is blank")
    args = parser.parse_args()

    if args.pendant and args.receiver:
        say("--pendant and --receiver install different entry points to the")
        say("same main.py, so pick one.")
        return 1

    if args.list:
        devices = board.attached()
        report_attached(devices)
        roles = describe_roles(devices)
        if roles:
            say()
            say("roles, as the boards themselves report them:")
            for serial, role in roles.items():
                say("  id:{:<18} {}".format(serial, role or "not set"))
        return 0

    if args.pendant:
        role = board.PENDANT_ROLE
    elif args.receiver:
        role = board.RECEIVER_ROLE
    else:
        say("What are you setting up?")
        role = choose("role", [
            ("pendant - the handheld with the wheel and the screen",
             board.PENDANT_ROLE),
            ("receiver - the board that plugs into the sender's PC",
             board.RECEIVER_ROLE),
        ])
        say()

    try:
        return run(role, args)
    except KeyboardInterrupt:
        say()
        say("stopped. Nothing is half-written: the board keeps whatever it had")
        say("unless an install had already started, and re-running is safe.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
