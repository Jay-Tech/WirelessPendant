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
> Target the board explicitly by its unique ID instead. The ID is held in one
> place, [`tools/board.py`](tools/board.py), and every tool resolves through it
> — so there is no serial number to copy from a docstring and mistype. It stays
> correct across COM port renumbering.
>
> Run it to see what is attached and which device is the target. It labels the
> controller so it cannot be picked by accident:
>
> ```bash
> python tools/board.py
> ```
>
> Override it without editing anything by setting `PICO_DEVICE`.
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
python tools/sync_board.py
```

```bash
python tools/on_board.py micropython/smoke_test.py
```

`run` streams the script from your PC rather than installing it, but imports
still resolve on the board — which is why `secrets.py` has to be copied over
first. To drop into the REPL instead:

```bash
python -m mpremote connect $PICO_DEVICE repl
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

## Pendant hardware summary

Everything the pendant needs, in one place. The reasoning behind each choice is
in the sections that follow; this is the part you need with a soldering iron in
your hand.

**Discrete parts: two resistors.** Nothing else is needed between the Pico 2 W
and the peripherals — no level shifters, no pull-ups, no transistors. The
encoder's 22 kΩ pair is the whole passive BOM, and the touch module does its own
level conversion on board.

| Pin | Net | Notes |
|---|---|---|
| GP2 | encoder A | via 22 kΩ to GND — see the encoder section |
| GP3 | encoder B | via 22 kΩ to GND |
| GP7 | feed hold | to GND, internal pull-up |
| GP8 | cycle start | to GND, internal pull-up |
| GP9 | zero axis | to GND, internal pull-up, long hold |
| GP10 | CTP_SDA | touch I²C |
| GP11 | CTP_SCL | touch I²C |
| GP12 | CTP_INT | read, not used as an interrupt |
| GP13 | CTP_RST | active low |
| GP17 | LCD_CS | |
| GP18 | SCK | shared SPI0 |
| GP19 | SDI / MOSI | shared SPI0 |
| GP20 | LCD_RS | the DC line |
| GP21 | LCD_RST | |
| GP22 | LED | backlight; leave open and it stays on |
| VBUS | encoder Vcc, display VCC | **5 V, not 3V3** |
| GND | encoder 0V, display GND | |

Free: **GP0, GP1, GP4–GP6, GP14–GP16, GP26–GP28**. GP4–GP6 came free when axis
and step selection moved to the touch panel, and GP26–GP28 are the ADC-capable
ones — the obvious home for a battery divider.

Unconnected on the display module: `SDO/MISO` and `SD_CS`. The pendant never
reads from the panel and does not use the SD slot.

**Both peripherals want 5 V.** The encoder is specified at 5 V, and the display
regulates its own 3.3 V on board — its manual is explicit that feeding it 3.3 V
leaves that rail short and dims the backlight. Logic stays at 3.3 V throughout:
the encoder is divided down, and the touch I²C is level converted on the module.

**Bringing a rebuilt harness up**, one layer at a time rather than all at once:

```bash
python tools/on_board.py micropython/pendant/probe_encoder.py      # characterise A/B
python tools/on_board.py micropython/pendant/selftest_quadrature.py
python tools/on_board.py micropython/pendant/selftest_st7796.py    # backlight, then pixels
python tools/on_board.py micropython/pendant/selftest_touch.py     # bus, then part, then touches
python tools/on_board.py micropython/pendant/selftest_link.py
```

Each separates the failures the one above it would otherwise mask.

## Wireless pendant (in progress)

> **Quietening the pendant for machine work.** Set `TRACE_DUMP_BUDGET = 0` in
> [`micropython/pendant/jog.py`](micropython/pendant/jog.py) to stop the trace
> tables. Everything is still counted and still reported in the periodic
> one-liner, so nothing is lost but the tables.
>
> It matters because printing can block. With no USB host attached MicroPython
> discards console output, so a pendant running standalone from `main.py`
> cannot stall on it however much it writes — but attached to `mpremote`, a
> host that is not draining fast enough blocks `print()`, and a blocked print
> blocks the whole event loop: the socket read and the jog scheduler with it.
> A run with seventy trace dumps stalled the loop for a full second.


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
python tools/on_board.py micropython/pendant/probe_encoder.py
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
python tools/on_board.py micropython/pendant/selftest_link.py
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
python tools/on_board.py micropython/pendant/selftest_quadrature.py
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
  secrets.example.py    WiFi credentials template -> copy to secrets.py
```

Credentials live in `secrets.py`, which is gitignored. `secrets.example.py` is
tracked — keep the placeholders in it and never put real values there.
