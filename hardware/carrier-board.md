# Pendant carrier board

> **Superseded by [platform-decision.md](platform-decision.md).** The pendant
> moves to an ESP32-S3 all-in-one board with a dedicated link, because an
> open-source pendant cannot require the builder to have usable WiFi - a
> requirement none of the reasoning below was weighed against.
>
> Kept rather than deleted. The electrical and mechanical work here is still
> correct and most of it carries over: the encoder dividers, the argument
> against running the display at 3V3, the dial overhang, the encoder bolt web,
> the button clearance, the wire slot. Only the board that hosts it changed.

First draft off the breadboard. The carrier holds the Pico 2 W, the display, the
two encoder dividers and the buttons, and takes its power from a LiPo Amigo Pro
mounted separately in the case.

Nothing here changes the firmware. Every pin is the one already in
`micropython/pendant/pendant.py`, so a board built to this spec runs the current
`main.py` with no edits — which is the point. The one addition is GP26 for
battery sense, on a pin that was already free.

## Why this shape

The decision to stay on the Pico 2 W rather than move to an all-in-one ESP32
board came down to two measurements, both in the repo's history:

- **Power.** The ESP32 board's real advantage was its AXP2101 doing charging,
  power path, fuel gauge and power button. The LiPo Amigo Pro does the first
  three of those plus the button, as a module, so that advantage mostly
  evaporated.
- **Radio.** Matched at −54 dBm on a dedicated 2.4 GHz channel, the two boards
  split evenly - the Pico owns the floor and median (8/61 ms against 18/75), the
  ESP32 the tail (p99 135 against 265). Neither is ahead by enough to justify
  re-validating a tuned system. `selftest_latency.py` reproduces this.

`quadrature_pcnt.py` stays in the tree as a tested fallback if this board turns
out worse than expected.

## Geometry

**162.00 x 56.00 mm**, 1.6 mm FR4. Two DXFs carry it, both in the same frame
with the origin at the display end - X runs 0 to -162, Y runs 0 to -56.

| File | Goes to | Contains |
|---|---|---|
| `Outline.dxf` | `Edge.Cuts` | board outline, the 8 x 5 mm wire slot, the 43 mm encoder cutout |
| `Holes.dxf` | a user layer, e.g. `Dwgs.User` | all 73 drilled positions, as a placement template |

`Outline.dxf` is generated from `Holes.dxf` by removing every circle except the
43 mm one, so the two cannot drift out of alignment. Exporting them separately
once produced a 180 degree frame flip that would have put every hole at the
wrong end of the board.

The split is a KiCad requirement: Import Graphics puts every entity on one
layer, so a combined file sent to `Edge.Cuts` yields a board outline plus 74
circular cutouts. **Imported DXF circles are drawings, not holes** - they drill
nothing. Real holes come from footprints, and `Holes.dxf` only says where to put
them.

| Feature | Position |
|---|---|
| encoder cutout | 43 mm dia at (-132, -28), 50.8 mm bolt circle, 3 x 3.5 mm |
| display mounts | 73.84 x 45.56 mm, 4 x 3.3 mm |
| button column | X = -92, buttons at Y -14/-28/-42, panel screws at -7/-21/-35 |
| wire slot | 8 x 5 mm at X -100..-92, Y -52.5..-47.5 |

Four drill sizes across the whole board - 3.5 x3, 3.3 x4, 2.3 x10, 1.0 x56.
Anything outside those four groups is a mistake, which makes the footprints easy
to check by eye.

**Why 56 mm wide.** It is sized to the display and its bezel. The 60 mm dial
therefore overhangs the board by 2 mm per side, which is deliberate rather than
an oversight: the dial sits above the board, and the case is built out to meet
its bezel flush. The enclosure is wider than the PCB at the wheel end by design.

**Why the buttons sit at X = -92.** They are squeezed between the display and
the dial, and because the dial is round it reaches furthest toward the display
exactly on the centreline - where the middle button is. At X = -92 the middle
button clears the dial rim by 10.0 mm and the display's mounting holes by 13.4
mm. Moving the column either way trades one against the other; no rearrangement
of three buttons widens the corridor they sit in.

## Power

> **Provisional.** This section is designed against the Waveshare module's
> schematic, not against a part on the bench. Confirm it once the display is
> wired and running - particularly the display's actual current at 3.3 V, which
> decides whether the Pico's regulator carries it.

The Amigo Pro has **no boost**. Its DEVICE connector is raw cell voltage,
3.0-4.2 V, limited to about 1 A by the XB6096I2S. The display runs at 3.3 V and
only the encoder wants 5 V, so the sole converter this board has to carry is a
small one for about 30 mA.

```
USB-C ─→ LiPo Amigo Pro ─→ DEVICE (JST-PH, 3.0-4.2 V)
         charge · power path · protection · on/off button
                                    │
                   ┌────────────────┼────────────────┐
                   │                │                │
              Pico VSYS       boost → 5 V      10k/10k → GP26
            (1.8-5.5 V in)          │         (battery sense)
                   │                └─→ encoder Vcc ─→ 22k ─→ GP2/GP3
              Pico 3V3 out
                   └─→ display VCC
```

**The Pico runs straight off the cell.** VSYS accepts 1.8-5.5 V and the Pico's
own buck-boost makes 3.3 V from it, so converting up to 5 V first would be two
conversions where one will do.

**The display hangs off the Pico's 3V3 pin**, for a reason that is not obvious:
a dedicated 3.3 V rail from a single cell cannot be an LDO or a plain buck,
because the cell falls to 3.0 V - below the output. It would have to be a
buck-boost, and the Pico already contains one sized for exactly this input
range, with headroom on the 3V3 pin for external draw. Putting a second one on
the carrier would be paying twice for the same converter.

Everything derives from the switched DEVICE output, so the rails rise and fall
together. That matters: a 5 V encoder feeding dividers into an unpowered Pico
would push current through its protection diodes.

### The Amigo Pro is not on this board

It is a 36 x 23 x 7 mm module that lives in the case. The carrier's entire
interface to it is **two wires, VDEV and GND**, into a 2-pin connector. Design
the enclosure around the module, not the board around the module.

It brings more out than its connectors suggest, and three of those change the
enclosure rather than the carrier:

| Interface | Use |
|---|---|
| DEVICE JST-PH, or the VDEV pad | 3.0-4.2 V into the carrier |
| BATTERY JST-PH, or the VBAT pad | the cell |
| **SW header** | external momentary on/off switch |
| **LED headers** | external charge and status indicators |
| USB-C | the only way power gets in |

So the power button is a **fourth panel-mount button**, wired to the Amigo's SW
header rather than to the Pico. It does not appear anywhere in the firmware and
needs no GPIO. The status LED wants a position on the case too.

**Align the module's own USB-C with a case cutout.** There is no alternative
power injection - USB-C is the only input - so a second receptacle on the
carrier would have to be wired back to the module's own connector pins, which is
a poor mechanical joint in something handheld. One cutout, no extra parts, no CC
resistors.

The battery charges with the output switched off, so the pendant can sit on the
charger overnight while turned off, which is the way it will actually be used.

### Sizing the boost

Estimates, worth confirming with a meter before ordering:

| Load | Rail | Draw |
|---|---|---|
| display logic + backlight | 3V3, from the Pico | 100–150 mA |
| touch controller | 3V3, from the Pico | ~5 mA |
| RP2350 + WiFi | internal | 50–120 mA |
| **encoder** | **5 V, from the boost** | **20–40 mA** |

The 5 V rail carries the encoder and nothing else, so almost any boost will do.
What was a 500 mA converter feeding the whole device is now about 30 mA.

The number still worth measuring is the **display's current at 3.3 V**. It sits
on the Pico's regulator alongside the RP2350 and the radio, and backlight
current rises as voltage falls for the same brightness. There is headroom on
that rail, but headroom is not a measurement.

**Buy the boost as a module for this board.** A boost is one of the easier
things to lay out badly - the switch node loop wants to be small, and a sloppy
one radiates into exactly the two nets that matter here, the display SPI and the
encoder inputs. Let someone else's layout carry that risk on the first
revision.

Prefer a **fixed 5 V** module over an adjustable one. The ubiquitous MT3608
boards set their output with a trimmer pot, which is a part that can be knocked
in a handheld tool that lives in a shop.

### Battery sense

10k/10k divider from the DEVICE rail to GP26, with 100 nF to ground at the pin.

Tapping **before** the boost is the whole point: a boost holds a contented 5 V
right up until it collapses, so sensing after it gives no warning at all. The
cell voltage is the only honest signal.

4.2 V through a 2:1 divider reads 2.1 V, using about two thirds of the ADC's
range. 10k/10k draws 210 µA, which is under 2 mAh across an eight hour shift and
is switched off with everything else. The 100 nF is not optional - the RP2350's
SAR wants a low impedance source for its sampling instant, and the cap supplies
the charge the divider cannot.

Firmware thresholds, once it exists: warn near 3.4 V, shut down near 3.2 V.

## Connections

Every pin below is already in `pendant.py`. GP26 is the only new one.

### Encoder — 4-pin connector

| Signal | Goes to | Note |
|---|---|---|
| A | GP2, via 22 kΩ to GND | divider — see README |
| B | GP3, via 22 kΩ to GND | |
| Vcc | 5 V rail | **not 3V3** |
| 0 V | GND | |

The handwheel is a push-pull line driver swinging a full 0–5 V, and the RP2350
is not 5 V tolerant. Its internal ~10 kΩ pull-up forms the upper half of the
divider; 22 kΩ puts the high level at 3.06 V, best centred against the input
threshold. The two 22 kΩ resistors are the entire passive BOM of the original
breadboard.

### Display — SPI

| Pin | Signal |
|---|---|
| GP17 | CS |
| GP18 | SCK |
| GP19 | MOSI |
| GP20 | DC |
| GP21 | RST |
| GP22 | backlight |
| — | VCC to 5 V rail, GND |

`SDO/MISO` and `SD_CS` stay unconnected - the panel is written to, never read,
and the SD slot is unused.

**Use Interface 1, the 18-pin 0.5 mm FPC.** The module offers two host
interfaces and both carry display and touch together, so either would work
electrically:

| | Interface 1 | Interface 2 |
|---|---|---|
| Type | 18-pin FPC, 0.5 mm | **JST GH**, 15-pin, 1.25 mm |
| Cable | flat flex, **supplied with the module** | crimped harness |
| Carrier part | 18-pos 0.5 mm ZIF socket | `SM15B-GHS-TB` header |
| Cable parts | none to buy | 2x `GHR-15V-S` + 30x `SSHL-002T-P0.2` |

Interface 2 looked the better choice while it was mistaken for a coarse-pitch
FPC - 1.25 mm is far kinder to solder than 0.5 mm, and a GH latch holds better
under vibration than a ZIF actuator. Two things settle it the other way.

**Height.** GH is wire-to-board. The module stands **10.3 mm tall including its
standoffs**, with both connectors on the underside roughly level with them, so
there is about 4 mm of gap. A mated GH plug is 3.4-5 mm before the wires have
bent, which does not fit. Flat flex is under 1 mm and turns tightly.

**The cable ships with the module.** That removes the three specs that would
otherwise each have to be guessed correctly - pitch, length, and whether
contacts sit on the same or opposite faces at the two ends. Two of those three
mirror the pinout silently when wrong.

What the supplied cable does decide is whether the socket must be **top or
bottom contact**. Do not reason it out: plug the cable into the module, route it
to where the socket will sit, and look at which face the exposed contacts
present. Measure its length too - it constrains how far from the module the
socket can be placed.

A ZIF actuator holds less firmly than a latch and this is a tool that gets
carried around a shop, so add a retainer or a dab of RTV once it is working. And
solder the socket before anything tall goes on the board, while an iron can
still lie flat.

One connector carries the whole module - display and touch both - and every
signal lands on a pin the firmware already uses:

| FPC | Signal | Goes to |
|---|---|---|
| 1 | VCC | Pico 3V3 |
| 2 | LCD_BL | GP22 |
| 3 | GND | GND |
| 4 | LCD_SCLK | GP18 |
| 5 | LCD_MOSI | GP19 |
| 6 | LCD_MISO | unconnected - the panel is written to, never read |
| 7 | LCD_DC | GP20 |
| 8 | LCD_RST | GP21 |
| 9 | LCD_CS | GP17 |
| 10 | SD_CS | unconnected - the card slot is unused |
| 11, 16-18 | NC | - |
| 12 | TP_RST | GP13 |
| 13 | TP_SCL | GP11 |
| 14 | TP_SDA | GP10 |
| 15 | TP_INT | GP12 |

Interface 2 additionally brings out the module's regulated **3V3** rail, which
Interface 1 does not. That is the LDO's output, so driving it externally would
bypass the regulator and recover the ~90 mV it drops. Not worth back-feeding a
regulator for, and moot on Interface 1.

**The hole pattern in Holes.dxf - 73.84 x 45.56 mm - comes from Waveshare's own
supplied CAD**, so it is already the right module. Still worth a caliper against
the physical part before the outline is committed: vendor CAD and vendor
hardware occasionally disagree, and a mounting pattern is an expensive thing to
find wrong after the boards arrive.

**The display runs at 3V3, and 5 V would be worse.** This is vendor-specific and
was decided the other way for the module that came first, so it is worth setting
down why it changed.

Both modules regulate VCC down to 3.3 V on board. The difference is the part.
LCDWiki's used an AMS1117-class LDO with about 1.1 V of dropout, so a 3.3 V
input could not produce 3.3 V and the backlight visibly dimmed. Waveshare's uses
an **ME6217C33M5G** - 3.3 V, 800 mA, **180 mV dropout at 300 mA**. At this
board's load the drop is nearer 90 mV, so 3.3 V in gives about 3.2 V on the
panel rail, inside spec for both the ST7796S and the FT6336U.

Feeding it 5 V is the worse option, not the safe one: that same LDO would
dissipate (5 - 3.3) x 150 mA, roughly **255 mW**, in a SOT-23-5 under the screen
of a sealed handheld - and you would have bought a 500 mA boost in order to
create the heat. At 3V3 the regulator sits in dropout doing almost nothing.

The module also carries a **TXS0108E** level translator. With VCC and the panel
rail both near 3.3 V it is essentially passing through, which is the kindest
case for it. If the panel ever shows corruption or fails to initialise, drop the
SPI baudrate before suspecting the driver - that part has a reputation for being
marginal on fast SPI.

### Touch — I2C1

| Pin | Signal |
|---|---|
| GP10 | SDA |
| GP11 | SCL |
| GP12 | INT |
| GP13 | RST |

Level converted on the touch module, so 3.3 V logic is safe against it.

### Buttons — to GND, internal pull-ups

| Pin | Action |
|---|---|
| GP7 | feed hold |
| GP8 | cycle start |
| GP9 | zero axis (long hold only) |

Panel-mount rather than PCB tactile: this is handled with gloves, with chips
about, and a panel button is the more robust part. Axis and step selection are
on the touch panel and need no pins.

There is a **fourth** panel-mount button, for power, but it belongs to the Amigo
Pro's SW header and touches neither this board nor the firmware. Worth counting
when laying out the case.

**No E-stop on this board.** Feed hold and cycle start over WiFi are fine -
worst case they are late. An E-stop that depends on an associated radio link is
not an E-stop, and a big red button teaches the hand the wrong reflex.

### Still free after this board

GP0, GP1, GP4, GP5, GP6, GP14, GP15, GP16, GP27, GP28.

## Layout notes

- **Socket the Pico.** 2×20 female headers. It makes the module replaceable,
  survives a bad joint, and leaves the carrier reusable.
- **Keep GP2/GP3 away from the SPI bus.** The display switches hard at tens of
  MHz next to the two most timing-critical inputs on the board. Opposite sides,
  ground pour between.
- **Design the outline to the display's mounting holes** and stack the carrier
  behind it. Board-to-board is stronger than a ribbon, and it fixes the
  enclosure dimensions for you.
- **Check the battery connector polarity.** JST-PH on LiPo cells is not
  standardised between vendors and plenty of boards have died to a reversed
  pack.

### Footprints to place and leave empty

They cost nothing unpopulated and each one saves a respin:

- Pull-ups on A/B to 3V3, in case the encoder is not the line driver it appears
  to be
- Series-R and cap pads for an RC filter on A/B
- ESD diodes on the encoder lines - a handwheel cable in a shop with a concrete
  floor is a real path into a GPIO

### Test points

The bring-up order in the README is only runnable with a meter if the nets are
reachable. Pads on: cell (DEVICE), 5 V, 3V3, encoder A and B raw, both divider
outputs, GND.

## Bring-up order

Same layering as the README, each step separating a failure the one above would
mask:

1. **Power only.** No Pico seated. Confirm the DEVICE rail, the 5 V rail, and
   that the Amigo's button gates both. Confirm charging works with the output
   off.
2. **Pico seated, nothing else.** `smoke_test.py`.
3. **Encoder.** `selftest_quadrature.py` for the decode, then
   `monitor_encoder.py` on the real wheel. Watch `errors` - it should stay at
   zero, and a climbing value means the dividers or the routing are wrong.
4. **Display.** `selftest_display.py`, then `selftest_st7796.py`.
5. **Touch.** `selftest_touch.py`.
6. **Link.** `selftest_link.py` against `tools/mock_sender.py`, then
   `selftest_latency.py` for a number to compare against the bench figures
   above.
7. **Whole thing.** `tools/run_pendant.py`.
