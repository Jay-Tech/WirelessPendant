# Pico 2 W

Experiments on a Raspberry Pi Pico 2 W (RP2350 + Infineon CYW43439).

## Board notes

| | |
|---|---|
| MCU | RP2350, dual Cortex-M33 @ 150 MHz **or** dual Hazard3 RISC-V |
| RAM / flash | 520 KB SRAM / 4 MB QSPI |
| PIO | 3 blocks, 12 state machines |
| Radio | WiFi 4 (802.11n, 2.4 GHz) + Bluetooth 5.2 |
| Power | 1.8-5.5 V into VSYS via onboard buck-boost |

Three things that bite people on this specific board:

- **The onboard LED is not a GPIO.** It hangs off the CYW43 radio chip, so it's
  `Pin("LED")`, never `Pin(25)`. Most Pico tutorials assume a non-W board and
  will silently do nothing.
- **GP29 belongs to the WiFi SPI**, so the usable ADC channels are GP26/27/28
  (channels 0-2). Channel 4 is the internal temperature sensor.
- **Erratum RP2350-E9.** On the A2 stepping, an input pin can latch near 2.15 V
  instead of reading low, and the internal pull-downs are too weak to fix it.
  This hits PIO input programs too. Use an external pull-down of 8.2 kΩ or
  smaller. Stepping A4 corrects it — worth checking which one your board has
  before you spend an evening debugging a "broken" input.

## Flashing MicroPython

1. Download the Pico 2 W `.uf2` from
   [micropython.org/download/RPI_PICO2_W](https://micropython.org/download/RPI_PICO2_W/)
   (current release: **v1.28.0**). Make sure it's the `RPI_PICO2_W` build —
   the plain `RPI_PICO2` build has no WiFi and the plain `RPI_PICO_W` build is
   for the older RP2040 board.
2. Hold **BOOTSEL** while plugging in USB. The board mounts as a drive named
   `RP2350`.
3. Copy the `.uf2` onto it. The board reboots into MicroPython automatically.

If the board misbehaves after switching between Arm and RISC-V builds, or
between MicroPython and C SDK firmware, flash `flash_nuke.uf2` first to wipe
the flash, then reflash. Leftover filesystem blocks confuse the new firmware.

## Running the smoke test

Uses [`mpremote`](https://docs.micropython.org/en/latest/reference/mpremote.html):

```bash
pip install mpremote
```

Commands below invoke it as `python -m mpremote` rather than the bare
`mpremote`. On Windows with Microsoft Store Python, the `Scripts\` directory
holding `mpremote.exe` is not on `PATH`, so the bare command fails with
*"not recognized as the name of a cmdlet"* even though the install succeeded.
The `-m` form sidesteps that and works on every platform. See
[PATH fix](#windows-store-python-path) if you want the short command back.

Set up credentials (this file is gitignored — it never reaches a commit):

```bash
cp micropython/secrets.example.py micropython/secrets.py
```

**Edit it before running anything.** Leaving the placeholder SSID in place
makes `wifi_join` fail with `no such network in range`. Then copy it to the
board and run the test:

```bash
python -m mpremote connect auto fs cp micropython/secrets.py :secrets.py
```

```bash
python -m mpremote connect auto run micropython/smoke_test.py
```

`run` streams the script from your PC rather than installing it, but imports
still resolve on the board — which is why `secrets.py` has to be copied over
first. To drop into the REPL instead:

```bash
python -m mpremote connect auto repl
```

To confirm the board is seen at all, and check what it's running:

```bash
python -m mpremote devs
```

A Pico 2 W shows a `2e8a:` vendor ID. PID `0005` means MicroPython is already
flashed and talking; PID `000f` means the board is sitting in RP2350 BOOTSEL
mode waiting for a UF2.

Prefer a GUI? [Thonny](https://thonny.org/) works well: set the interpreter to
*MicroPython (Raspberry Pi Pico)* and it handles the board filesystem for you.

### What it checks

Six stages, each reporting PASS / FAIL / SKIP with a summary at the end:

| Stage | Proves |
|---|---|
| `identity` | firmware, chip ID, clock, heap, filesystem |
| `led` | GPIO path via the CYW43 — visible blink |
| `temp` | ADC works (internal sensor, uncalibrated, ±a few °C) |
| `wifi_scan` | radio powers up and hears networks |
| `wifi_join` | association + DHCP lease |
| `net_io` | DNS + TCP + HTTP, end to end |

The last three need `secrets.py`; without it they report SKIP and the rest
still runs, so the script is useful before you've picked a network.

## Serial → WiFi bridge

[`micropython/serial_bridge.py`](micropython/serial_bridge.py) turns the Pico
into a transparent bridge between a grblHAL/GRBL controller's UART and TCP,
making a USB-only board networked:

```
GRBL controller  --UART-->  Pico 2 W  --TCP:23-->  sender application
                 <--UART--            <---------
```

It listens on **port 23**, grblHAL's telnet convention, and moves raw bytes
with no interpretation — so a sender that already speaks TCP connects to it
unmodified.

### Wiring

| Pico | | Controller |
|---|---|---|
| GP0 (pin 1) | TX → | RX |
| GP1 (pin 2) | RX ← | TX |
| GND (pin 3) | — | GND |

Common ground is required, not optional — without it the UART sees noise.

> **3.3 V only.** The RP2350 is not 5 V tolerant. Most grblHAL boards (STM32,
> Teensy, ESP32) are 3.3 V and wire straight through. A 5 V controller — an
> Arduino Uno/Nano running classic GRBL — needs a level shifter on the Pico's
> RX line or it will damage the pin.

### Test it with no controller attached

Jumper **GP0 to GP1** and the bridge echoes back everything you send, which
proves the whole path (TCP in → UART out → UART in → TCP out):

```bash
python -m mpremote connect auto run micropython/serial_bridge.py
```

Then from another terminal, connect with any TCP client and type — it comes
straight back.

### Run it

```bash
python -m mpremote connect auto run micropython/serial_bridge.py
```

To start automatically on power-up, copy it as `main.py` instead:

```bash
python -m mpremote connect auto fs cp micropython/serial_bridge.py :main.py
```

Then point the sender's TCP adapter at the board's address on port 23. The
onboard LED reports state at a glance:

| LED | Meaning |
|---|---|
| Fast blink | No WiFi — reconnecting |
| Slow heartbeat | Online, no client connected |
| Solid | Client attached, bridging |

### Design notes

- **Byte-level, never line-buffered.** GRBL's real-time commands (`?`, `!`,
  `~`, `0x18`, and the `0x8x` overrides) are single bytes that arrive mid-line
  and must act immediately. Waiting for `\n` would break status polling and
  delay a feed hold.
- **Nagle is disabled** (`TCP_NODELAY`). Otherwise a lone `?` gets held back
  waiting for company, adding tens of ms to every status poll and making the
  DRO stutter. If the socket option isn't available the bridge logs a warning
  and continues.
- **One client at a time; newest wins.** GRBL is single-session — two senders
  would interleave commands and corrupt parser state. New connections evict
  the old one because the usual cause is a stale half-open socket from a
  crashed client, and refusing would lock you out until it timed out.
- **UART is drained when nobody's connected**, so a session always starts on a
  clean message boundary instead of mid-line. Those bytes are counted as
  `dropped` in the periodic stats line.
- **WiFi self-heals.** A watchdog rejoins on link loss — it's going to be
  bolted to a machine, not sitting on a desk.

## Windows Store Python PATH

Optional — only if you want to type `mpremote` instead of `python -m mpremote`.

Microsoft Store Python installs console scripts into a sandboxed per-user
directory that the Store's PATH shim doesn't cover. Find it with:

```bash
python -c "import sysconfig; print(sysconfig.get_path('scripts', 'nt_user'))"
```

Then append that one directory to your **user** PATH (this reads and writes
only the user scope, so it can't clobber the machine PATH):

```powershell
$s = python -c "import sysconfig; print(sysconfig.get_path('scripts','nt_user'))"; [Environment]::SetEnvironmentVariable('Path', [Environment]::GetEnvironmentVariable('Path','User') + ';' + $s, 'User')
```

Restart the terminal afterwards. Existing sessions keep the old PATH.

## Layout

```
micropython/
  smoke_test.py         staged board bring-up check
  serial_bridge.py      transparent UART <-> TCP bridge for grblHAL
  secrets.example.py    WiFi credentials template -> copy to secrets.py
```

Credentials live in `secrets.py`, which is gitignored. `secrets.example.py` is
tracked — keep the placeholders in it and never put real values there.
