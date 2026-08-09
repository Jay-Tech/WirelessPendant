# Pendant carrier board

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

## Power

The Amigo Pro has **no boost**. Its DEVICE connector is raw cell voltage,
3.0–4.2 V, limited to about 1 A by the XB6096I2S. Both the display and the
encoder want 5 V, so a boost is the one converter this board has to carry.

```
USB-C ─→ LiPo Amigo Pro ─→ DEVICE (JST-PH, 3.0–4.2 V)
         charge · power path · protection · on/off button
                                    │
                   ┌────────────────┼────────────────┐
                   │                │                │
              Pico VSYS        boost → 5 V      10k/10k → GP26
            (1.8–5.5 V in)          │          (battery sense)
                                    ├─→ display VCC
                                    └─→ encoder Vcc ─→ 22k dividers ─→ GP2/GP3
```

**The Pico runs straight off the cell, not off the 5 V rail.** VSYS accepts
1.8–5.5 V and the Pico's own buck-boost makes 3.3 V from it, so routing it
through the boost first would be two conversions where one will do - roughly
90% against 78% for the Pico's share of the load. The boost then only has to
carry the display and the encoder.

Both rails come from the switched DEVICE output, so they rise and fall together.
That matters: a 5 V encoder feeding dividers into an unpowered Pico would push
current through its protection diodes.

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

| Load | at 5 V |
|---|---|
| display logic + backlight | 100–150 mA |
| encoder | 20–40 mA |
| touch controller | ~5 mA |

Call it 200 mA typical and 300 mA peak on the 5 V rail. Drawn from a 3.7 V cell
at ~88% that is around 460 mA, and near 550 mA with the cell down at 3.2 V. The
Pico adds 50–120 mA directly. Comfortably inside the Amigo's ~1 A ceiling, but
the headroom shrinks as the cell drains, which is the wrong end to discover it.

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

**Place both the 2.54 mm header and the 14-pin FPC footprint.** The module
brings the same fourteen signals out on either, and unpopulated pads cost
nothing. Which one gets fitted is a mechanical decision that cannot be made
until the outline exists: a stacked 2.54 mm pair is about 8.5 mm plus
clearance, against 1-2 mm for an FPC connector whose cable also flexes, so the
carrier no longer has to sit rigidly behind the panel. On a handheld that is
most of a centimetre of depth.

Populate the header first. FPC has three ways to cost a board revision that a
0.1 inch header does not - the pitch is 0.5 mm or 1.0 mm and the footprints are
not interchangeable, cables come with contacts on the same or opposite sides at
each end, and connectors come in top and bottom contact. Two of those three
mirror the pinout silently.

**The display could run at 3V3, and should not.** LCDWiki document the module as
accepting either, but note that a 3.3 V input cannot hold a full 3.3 V on the
regulator's output, and the backlight - transistor-driven from the LED pin -
dims as a result. Dropping the display to 3V3 would leave the encoder as the
only 5 V load at about 30 mA and shrink the boost to something trivial, which is
tempting until you are reading a DRO at the machine under shop lighting. Not a
trade worth one fewer part.

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
