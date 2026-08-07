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

> ### ⚠️ Do not use `connect auto` on a machine with a CNC controller attached
>
> `auto` connects to the first USB serial device it finds. If a grblHAL
> controller is plugged in, that can be the controller rather than the Pico —
> and mpremote will then send raw-REPL control bytes (`Ctrl-A`/`Ctrl-C`) to
> your machine controller trying to get a Python prompt. It happened here: the
> STM32 board on `0483:5740` got grabbed instead of the Pico and reset.
>
> Target the board explicitly by its unique ID instead. This board is
> `id:7BE7DD09548134C0`, which is what every command below uses, and it stays
> correct across COM port renumbering:
>
> ```bash
> python -m mpremote devs
> ```
>
> Pico boards report a `2e8a:` vendor ID (`0005` = MicroPython running,
> `000f` = RP2350 BOOTSEL). An STM32 grblHAL board reports `0483:5740`. Use
> the serial number in the second column as `id:<serial>`.

Set up credentials (this file is gitignored — it never reaches a commit):

```bash
cp micropython/secrets.example.py micropython/secrets.py
```

**Edit it before running anything.** Leaving the placeholder SSID in place
makes `wifi_join` fail with `no such network in range`. Then copy it to the
board and run the test:

```bash
python -m mpremote connect id:7BE7DD09548134C0 fs cp micropython/secrets.py :secrets.py
```

```bash
python -m mpremote connect id:7BE7DD09548134C0 run micropython/smoke_test.py
```

`run` streams the script from your PC rather than installing it, but imports
still resolve on the board — which is why `secrets.py` has to be copied over
first. To drop into the REPL instead:

```bash
python -m mpremote connect id:7BE7DD09548134C0 repl
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

### Does your controller actually need this?

The bridge assumes a controller with an **exposed spare UART**. Many grblHAL
boards don't have one, and two common ones don't:

| Board | Verdict |
|---|---|
| **Sienci SLB / SLB-EXT** | **Not needed.** Has Ethernet and runs grblHAL's networking plugin, already serving telnet on port 23. Point the sender straight at it. |
| **Expatria Flexi-HAL** | **Not usable.** No spare UART header. Expansion is an I2C real-time control port, RS485 for VFD, RJ45s for encoder/buttons/limits, and an RPi GPIO header wired for Remora SPI. USB-C is the only comms path. |

grblHAL's MPG mode needs a serial stream plus a mode-switch pin, so a board
with no broken-out UART can't use it however the firmware is compiled.

This bridge is for controllers that *do* expose a UART. If yours doesn't, the
options are the I2C keypad/pendant interface (real-time commands only — jog and
overrides, not G-code streaming), or USB host on the RP2350 to bridge the
controller's own USB port, which is a C SDK / TinyUSB project rather than a
MicroPython one.

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
python -m mpremote connect id:7BE7DD09548134C0 run micropython/serial_bridge.py
```

Then from another terminal, connect with any TCP client and type — it comes
straight back.

### Run it

```bash
python -m mpremote connect id:7BE7DD09548134C0 run micropython/serial_bridge.py
```

To start automatically on power-up, copy it as `main.py` instead:

```bash
python -m mpremote connect id:7BE7DD09548134C0 fs cp micropython/serial_bridge.py :main.py
```

Then point the sender's TCP adapter at the board's address on port 23. The
onboard LED reports state at a glance:

| LED | Meaning |
|---|---|
| Fast blink | No WiFi — reconnecting |
| Slow heartbeat | Online, no client connected |
| Solid | Client attached, bridging |

### Measured performance

Loopback (GP0→GP1), 50 status polls plus a 4880-byte / 200-line G-code stream:

| | |
|---|---|
| Round trip, median | **7.5 ms** |
| Round trip, p95 | **18.9 ms** |
| Round trip, worst | ~1000 ms (see below) |
| Stream integrity | byte-for-byte identical, every run |

Median and p95 are comfortably inside a 10–20 Hz status poll. Integrity never
failed — no dropped or reordered bytes in any test, including bare `0x18` and
newline-less `?` bytes.

**About the ~1 s outliers.** They are not the bridge. ICMP ping to the same
board, which is answered by lwIP without touching any Python, measures a
median of 5 ms and p95 of 16 ms — but shows **1% packet loss** over 200
packets. One lost TCP segment costs a retransmission timeout of roughly a
second, which is exactly the outlier size observed. The cause is 2.4 GHz
congestion: a scan here found four APs sharing channel 6 with this network.

If it bothers you, move the AP to a clearer channel (channel 1 was far quieter
in the scan) or reposition the board. Don't go looking for it in the code.

> **This bridge is not a safety path.** With any packet loss, a feed hold sent
> over WiFi can arrive a second late. That is fine for jogging, streaming, and
> status, and not fine as an emergency stop. Keep a hardwired physical E-stop
> on the machine — WiFi is a convenience layer, never the thing standing
> between you and a crash.

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
- **Radio power saving is off.** The CYW43 defaults to dozing between beacons,
  which is right for a sensor posting once a minute and wrong here — it adds
  latency on top of the baseline for no benefit on a mains-powered bridge.
- **The status LED is written only on state change.** It lives on the CYW43,
  not an RP2350 GPIO, so every write is an SPI transaction contending with the
  radio. Rewriting "on" every 200 ms while a client was attached measurably
  hurt the latency tail — fixing that moved p95 from 63.6 ms to 18.9 ms. Set
  `STATUS_LED = False` to remove it from the picture entirely.

## Wireless pendant (in progress)

A handheld MPG pendant — handwheel, buttons, display — that talks over WiFi to
the **sender application**, not to the controller:

```
  Pico 2 W pendant  --WiFi-->  GrblHAL Sender (PC)  --Ethernet/USB-->  controller
```

### Why not straight to the controller

The obvious design — pendant connects directly to the machine over the network —
does not work, for two independent reasons:

- **grblHAL's telnet server accepts exactly one client.** `telnetd.c` refuses a
  second connection outright (`if(session->pcb) return ERR_CONN;`). The sender
  already holds that session, so the pendant cannot have one.
- **On an SLB, the board isn't reachable anyway.** Sienci documents a direct
  PC-to-board Ethernet cable on a static `192.168.5.x` subnet. That link is an
  isolated segment with no route from the WiFi network.

Going through the sender avoids both, works with any controller, and keeps the
sender as the single arbiter of the command queue — which is what GRBL requires
regardless of transport.

### Encoder wiring

> **The RP2350 is not 5 V tolerant.** A 5 V handwheel will damage a GPIO if
> wired straight in. Identify the output type first.

Count the terminals:

| Terminals | Type | Wiring |
|---|---|---|
| **6** — A, B, 0V, Vcc, A-, B- | Differential **line driver**, push-pull, swings a full 0–5 V | Divider on A and B (below), or an RS-422 receiver |
| 4 — A, B, 0V, Vcc | Single-ended; still need to know which kind | Open-collector: 4.7 kΩ pull-up to **3.3 V**. Push-pull: divider as below |

**Six terminals does not automatically mean a line driver.** The wheel used
here has A, B, 0V, Vcc, A-, B- and turned out to be **open-collector with an
internal pull-up**, measured at ~13.3 kΩ. Characterise before wiring:

```bash
python -m mpremote connect id:7BE7DD09548134C0 run micropython/pendant/probe_encoder.py
```

An open-collector output already has a pull-up forming the top leg of a
divider, so adding an external series resistor only drops the level further.
One resistor to ground is the whole circuit:

```
  encoder A ──┬────────────────> GP2
             [22k]
              │
             GND                 (same again for B -> GP3)

  encoder 0V  ──────────────────> GND  (pin 38)
  encoder Vcc ──────────────────> VBUS (pin 40, USB 5 V)
```

4.909 V × 22/(13.3+22) = **3.06 V**. The level has to clear the RP2350's
input-high threshold (~2.15 V) without passing its absolute maximum of
**IOVDD + 0.3 V = 3.6 V**.

| To ground | Level | |
|---|---|---|
| 10k | 2.11 V | fails — below input-high |
| 20k | 2.95 V | works |
| **22k** | **3.06 V** | best centred, tolerant of pull-up variation |

Powering the encoder at 3V3 instead, to skip the resistor entirely, is not
worth it: the part is specified at 5 V and an under-driven encoder that works
intermittently is the worst outcome available.

A- and B- stay unused. They exist for noise immunity over a long cable; if
`errors` climbs because the lead runs near a VFD or steppers, feed A/A- and
B/B- into a 3.3 V RS-422 receiver and take single-ended out.

Erratum E9 is not a factor: the input is driven, not resting on a weak
pull-down.

Defaults are GP2 = A, GP3 = B.

A- and B- go unused in this arrangement. They exist for noise immunity over a
long cable; if `errors` starts climbing because the lead runs near a VFD or
steppers, feed A/A- and B/B- into a 3.3 V RS-422 receiver (MAX3095 or similar)
and take single-ended 3.3 V out. Not needed for a short bench lead.

Erratum E9 is not a factor either way here, since the input is actively driven
rather than resting on a weak pull-down.

Defaults are GP2 = A, GP3 = B.

**Check before connecting:** power the encoder from 5 V and GND only, and
measure A against 0V while turning slowly. It should swing hard between ~0 V
and ~5 V. If it never reaches 5 V unaided it is open-collector, not a line
driver, and wants pull-ups instead of dividers.

### Network link

[`link.py`](micropython/pendant/link.py) joins WiFi and holds a TCP session to
the sender, reconnecting on its own. The pendant is the client: the sender is
a fixed always-on machine, the pendant is the thing that wanders off and gets
switched off.

Three things it handles that a plain socket does not:

- **Silent death.** A session whose peer vanished — PC asleep, AP dropped —
  stays open for minutes before the stack notices, which on a pendant reads as
  a handwheel that has simply stopped working. A ping every 3 s plus a 10 s
  receive deadline turns that into a fast, visible reconnect.
- **Backpressure.** The send queue is bounded and drops oldest. A pendant that
  can't reach the sender must never build a backlog of stale jog commands that
  all execute at once when the link returns.
- **Flush before close.** Closing a socket discards whatever is still queued,
  so `flush()` exists and any orderly shutdown must call it. Skipping it loses
  the last few messages silently, after they have already been counted as sent.

### Mock sender

[`tools/mock_sender.py`](tools/mock_sender.py) stands in for the real sender:
it accepts a pendant, applies incoming jogs to a simulated DRO, and streams
status back at 10 Hz. Turn the handwheel and the position moves.

```bash
python tools/mock_sender.py --verbose
```

Then, with `SENDER_HOST` set in `secrets.py`:

```bash
python -m mpremote connect id:7BE7DD09548134C0 run micropython/pendant/selftest_link.py
```

The self-test drives synthetic handwheel motion — an out-and-back sweep on X, a
one-way Z move, buttons — and checks the DRO the sender reports back actually
follows. It needs no encoder or display wired.

### Running it

```bash
python tools/run_pendant.py
```

Syncs the board then starts the pendant. Both steps in one because forgetting
either is silent and misleading: no sync runs stale modules, and no run looks
exactly like a pendant that is broken rather than one that was never started.

Nothing auto-starts — there is deliberately no `main.py`, so the board always
boots to a free REPL. Copying `pendant.py` as `main.py` would make it run at
power-up, but it also holds the REPL from boot, so recovering means catching
the gap before the script starts or reflashing. Worth an escape hatch (skip
startup if a button is held) before doing that.

### Jog behaviour — open question

Two philosophies, and which feels right is a question for a real machine:

- **Velocity-follow** (current default). Stopping the wheel flushes queued
  motion and halts. The machine never runs on past your hand. Ten fast clicks
  may travel less than ten steps' worth, because the remainder is cancelled.
- **Queue-and-execute.** Every detent is honoured exactly, so ten clicks is
  always ten steps. The machine lags a fast spin and keeps moving after you
  stop.

Velocity-follow is the default because a handwheel is used to position by eye —
if you want an exact distance you would type it. Step size (the two step
buttons) is the coarse/fine control, which may well cover the "spin fast for
rough distance, crawl for fine" need without a second mode.

Compare them on a running pendant without reflashing:

```python
scheduler.cancel_on_stop = False   # queue-and-execute
scheduler.cancel_on_stop = True    # velocity-follow
```

The cancel threshold is `IDLE_MS_BEFORE_CANCEL`, 300 ms. It has to clear a slow
turn: someone winding continuously still produces a detent every few hundred
ms, and a shorter threshold fires between individual detents and truncates jogs
mid-move.

### Verifying the decoder

Decode logic runs on a PC with no board attached — it stubs `machine` and
drives the real class, so the test can't drift from the code:

```bash
python tools/test_quadrature.py
```

The hardware path (pin config, hard IRQ dispatch, speed headroom) needs two
jumpers, GP16→GP2 and GP17→GP3, which synthesise quadrature and read it back:

```bash
python -m mpremote connect id:7BE7DD09548134C0 run micropython/pendant/selftest_quadrature.py
```

It reports where edges start getting missed. A hand-turned 100 PPR wheel peaks
around 2000 edges/s, so there should be a lot of margin above it.

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
