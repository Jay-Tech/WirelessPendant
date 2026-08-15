# Pendant daughter board

Breaks the ESP32-S3-Touch-LCD-3.5's 32-pin header out to the handwheel and the
three panel buttons, and passes the battery through a reverse-polarity guard on
its way to the host board.

The breakout half carries no parts at all - the 3V3 encoder change removed the
last two resistors. The guard adds a MOSFET and one resistor, which is the
entire active BOM.

It exists because the host board brings almost everything out on that one
header - the only other connectors are JST leads for the speaker, the RTC cell
and the battery, none of which the pendant uses through here. So a single
mating board reaches every signal the pendant needs, and the alternative is
seven flying leads soldered to a header, in a tool that gets carried around a
shop.

> **Status: specification, not a built board.** Nothing here has been fabricated.
> The electrical side is settled and measured; the mechanical side is
> constrained but not dimensioned, because the enclosure is being drawn in
> Fusion 360 in parallel and the two have to agree.

## Every net on the board

Seven signals. Taken from `micropython/pendant/pendant.py`, which is the
authority - if this table and that file disagree, the file is right.

| Net | Host pin | Goes to | Note |
|---|---|---|---|
| Encoder A | GP9 | encoder connector | direct, no resistor |
| Encoder B | GP10 | encoder connector | direct, no resistor |
| Encoder Vcc | **3V3** | encoder connector | **not 5 V, not VBAT** |
| Encoder 0V | GND | encoder connector | |
| Feed hold | GP38 | button 1 | other side to GND |
| Cycle start | GP39 | button 2 | other side to GND |
| Zero axis | GP40 | button 3 | other side to GND, long hold |

Buttons are active low against the ESP32's internal pull-ups. No external
pull-ups, no debounce parts - `buttons.py` debounces in software and has been
doing so on real hardware since the Pico build.

**Confirm all five GPIOs are on the 32-pin header before laying out.** They
should be, since the bench harness reaches them today, but a signal that turns
out to live on a different connector is a respin, and it costs one minute with
a meter to rule out.

## Why the encoder is on 3V3, and why that matters here

Measured 2026-08-15. See [platform-decision.md](platform-decision.md) for the
full argument; the part that constrains this board:

- The handwheel is **open-collector with an internal pull-up**, so its output
  level is whatever its supply is. On 3V3 it presents a clean 3.3 V high
  straight to the GPIO.
- **No divider.** The 22 kOhm pair that the 5 V build needed is gone. Keeping
  it at 3V3 would give ~2.06 V, under the ESP32-S3's ~2.48 V input-high
  threshold, and would fail intermittently rather than outright.
- **Do not run the encoder from VBAT.** The level follows the supply, so a
  charged cell puts 4.2 V on a pin whose absolute maximum is ~3.6 V, and it
  sags as the cell drains. The regulated rail holds it still.

Evidence: 4,173 detents and 16,695 counts over thirty seconds at 100-195
detents/s with nothing dropped; low 0.12 V, high >= 3.22 V, duty 48/52 and
49/51.

**The 3V3 pin on the header carries the encoder's 20-40 mA** on top of whatever
the host board already draws through it. Almost certainly fine, but it is the
one current path on this board worth a glance at the host's trace before
committing.

## The header

The mating connector has to be taken from the part, not from vendor CAD or a
wiki page - pitch, row count, and pin 1 position. The display pin map for this
board came from Waveshare's demo code rather than their documentation, and a
header is the kind of thing that gets drawn optimistically.

**Make it impossible to mate rotated or offset.** A plain 2xN header will mate
180 degrees round, or one column out, and both put 3V3 somewhere it should not
be. Any one of these is enough:

- a shrouded, keyed header
- a standoff pattern that is deliberately asymmetric
- a mechanical feature on the enclosure that only allows one orientation

Silkscreen alone is not enough. It is on the side you cannot see once the
boards are stacked.

**Use several ground pins, not one.** The header will have more than one; tie
them all. It costs nothing and it is the difference between a ground return and
a ground wire.

## Buttons on the board, not on the panel

This reverses [carrier-board.md](carrier-board.md), which specified panel-mount
buttons because *"this is handled with gloves, with chips about, and a panel
button is the more robust part"*. That reasoning still stands on its own terms.
What changed is that putting them on the daughter board removes three pairs of
flying leads and a separate assembly step, and the robustness can be bought
back mechanically instead.

Two things decide whether it holds up:

**Side load is what kills tactile switches.** A gloved thumb pushes at an angle,
and a 6 mm tactile switch has almost no lateral tolerance. So the cap must be a
**plunger guided by a bore in the enclosure top**, not a spacer resting on the
switch. Then the case takes the side load and the switch only ever sees axial
force. This is the single most important mechanical detail on the board.

**The tolerance stack has to end in clearance, not crush.** Board seating height
through the header, plus switch height, plus cap, against the enclosure top.
Design the cap so it **bottoms on the enclosure** at the end of its stroke, with
the switch reaching its own click before that. A stack that ends the other way
holds three switches permanently pressed, which reads as a pendant that will not
stop asking for a feed hold.

Prefer a switch with a long travel and a firm, unambiguous click. Through a
glove and a printed cap, a short-travel switch gives no feedback at all, and the
operator presses it twice.

## Connectors

| For | Suggested | Why |
|---|---|---|
| Encoder | 4-pin JST-PH or XH | inside the enclosure, so latch and pitch matter more than height |
| Header | mirror the host board | measured from the part |

The handwheel's own cable decides the encoder connector as much as this board
does. If it arrives with flying leads, crimping them into a housing is kinder
to future disassembly than soldering them to the board - the wheel is the part
most likely to be swapped.

## Footprints to place and leave empty

They cost nothing unpopulated and each one saves a respin. Carried over from
[carrier-board.md](carrier-board.md), and the first is newly relevant because
the resistors it refers to were just removed by measurement:

- **22 kOhm pads on A and B to ground.** The 3V3 result is one wheel on one
  bench. A different handwheel, or a push-pull one, may want them back.
- **Pull-ups on A and B to 3V3**, in case a wheel turns out not to have an
  internal one.
- **Series-R and cap pads for an RC filter** on A and B.
- **ESD diodes on the encoder lines.** A handwheel cable in a shop with a
  concrete floor is a real path into a GPIO, and this is the board that path
  now runs through.

## Test points

Reachable pads on: 3V3, GND, encoder A, encoder B, and each button line. The
bring-up order below is only runnable with a meter if the nets are reachable,
and once the two boards are stacked almost nothing else is.

## What does not belong on this board

- **Anything at 5 V.** There is no 5 V anywhere in this design. The AXP2101's
  outputs all step down, and the boost that used to be in the BOM is gone.
- **The battery, unguarded.** It reaches the host board's own JST connector, but
  through this board rather than past it - see the reverse-polarity guard below.
  Nothing on this board draws from it.
- **The power button.** It is on the host board and is now reached through the
  enclosure rather than wired to a header pin.

## The battery extension is the most dangerous part of the build

Not on this board, but it belongs with it because nothing else in the pendant
can destroy the host board on first power-up.

The cell reaches the host board's JST connector through an extension. **An
extension is exactly where polarity gets inverted**: a two-wire lead with a
housing on each end comes out straight or mirrored depending on which face the
contacts are crimped to, and the two look identical. Battery connector polarity
is not standardised between vendors.

**Meter the host board's pads against its silkscreen, and the cell, before the
first mating.** A reversed LiPo into the AXP2101's battery input is a dead board
and possibly a vented cell.

Once it is connected, `selftest_battery.py` confirms the chain non-destructively
- but only after it is plugged in, so the meter comes first.

## The reverse-polarity guard

Two parts, and they turn "meter it every time" into "it does not matter". The
cell comes into a JST on this board, through the guard, and out of a second JST
to the host board's battery connector.

### The circuit

```
   cell +  ──┬── [D]  P-FET  [S] ──┬──  to host BAT+
             │         │           │
    JST in   │        [G]          │   JST out
             │         │           │
   cell −  ──┼─────────┴── 100k ───┼──  to host BAT−
             │                     │
            GND ══════════════════ GND
```

| FET pin | Connects to |
|---|---|
| **D** (drain) | cell positive, from the input JST |
| **S** (source) | output positive, to the host board |
| **G** (gate) | ground, through 100 kOhm |

**Drain to the cell, source to the host.** Wired the other way round it looks
identical and protects nothing - see the reasoning below. This is the single
detail to get right, and it is worth checking against the part's own datasheet
pinout rather than a generic symbol, because SOT-23 pin numbering is not
consistent between manufacturers.

Ground is common throughout - the guard is in the positive line only.

### What to buy

A **P-channel MOSFET, logic level**, judged on one number: it must be fully on
at **Vgs = -3.0 V**. Not -4.5 V, not -10 V, which is how most of them are
specified.

That number is not arbitrary. The gate sits at ground, so the FET is only ever
turned on as hard as the cell voltage, and at the end of a discharge that is
3.0 V. A part specified at -4.5 V will be part-way on down there - warm, and
dropping voltage - exactly when the battery is already low.

| Requirement | Why |
|---|---|
| P-channel, logic level | on at Vgs = -3.0 V |
| Rds(on) low, ideally under 50 mOhm at -2.5 V | see the note on the battery readout |
| Vds at least 20 V | trivially met; a 1S cell never exceeds 4.2 V |
| SOT-23 or similar | it dissipates almost nothing |

`AO3401A` and `DMG3415U` are both in the right class and both common. No Zener
across the gate is needed: 4.2 V is nowhere near a typical +/-20 V Vgs rating.

The 100 kOhm gate resistor is not critical - anything from 10 k to 1 M works. It
exists to give the gate a defined potential, not to set a speed.

### Prove it before it meets the host board

Do this with the guard board alone, **nothing connected to the output JST**
except a meter. This is the whole point of building it, and it takes a minute.

1. **Correct polarity in.** Output should read the cell voltage, within a few
   tens of millivolts. A drop of several hundred millivolts means the FET is not
   turning on - wrong part, or the gate resistor is not reaching ground.
2. **Reversed in.** Output should read **0 V**, and must not read negative.
   Brief - a second is enough to see it.
3. **Correct polarity again**, and confirm it comes back. A guard that only
   works once has failed the reversal rather than survived it.

Only after all three does the output JST get connected to the host board.

If you have a current-limited bench supply, use that for step 2 rather than the
cell. If you only have the cell, keep it short.

### Why a diode will not do, and why this orientation

Here for when the schematic is in front of you. Skip it if the circuit above is
already wired.

**The battery line runs both ways.** The AXP2101 charges the cell through the
same two pins it discharges it through. A series diode blocks charging outright,
and its 0.3-0.7 V drop would be ruinous on a cell whose whole useful span is
3.0 to 4.2 V. A MOSFET channel, once turned on, conducts in both directions,
which is what makes it the right part here.

**Normal polarity.** The body diode inside the FET points from drain to source,
so it conducts cell to host on its own. The output rises, which puts the source
at about 3.7 V while the gate is at 0 V, so Vgs is about -3.7 V and the FET
turns hard on - shorting out its own body diode and removing the diode drop.

**Reversed.** The input now sits 3.7 V *below* ground. The gate and source are
both near 0 V, so Vgs is 0 and the channel is off; and the body diode is now
reverse biased, so it blocks too. Nothing conducts.

**Wired the other way round** - source to the cell, drain to the host - the
channel still turns off on reversal, but the body diode ends up forward biased
and passes the reversed voltage straight through. The board looks identical and
protects nothing, which is why the orientation is worth checking twice.

### It changes what the battery gauge reads

The AXP2101 measures the battery at *its* pin, which is now on the far side of
the FET. So the voltage it reports is the cell plus or minus `I x Rds(on)`,
depending on whether it is charging or discharging - and the pendant's charge
percentage is derived from that voltage, so the error lands on the number the
operator reads.

| Rds(on) | Current | Error | On the curve |
|---|---|---|---|
| 50 mOhm | 0.5 A | 25 mV | lost in the noise |
| 50 mOhm | 2 A | 100 mV | ~10 points on the flat middle |

Keep Rds(on) low and it does not matter. The charge current the AXP2101 is
actually set to lives in registers `0x62` and `0x63`, which
`probe_axp2101_map.py` already dumps.

### What it does not protect

**Only what is upstream of it.** The input JST catching a mirrored extension is
the case this solves. The **output lead**, from this board to the host's battery
connector, is now the unprotected link - so make that one short, fixed, and
metered once, rather than treating it as something to unplug routinely.

**Not a short circuit.** Reverse polarity is the dead-board failure; a
downstream short is the fire one. That is the cell's protection PCB's job - most
1S packs have one at the terminals. **Confirm yours does.** If it is a bare
cell, a fuse in this line is worth more than the FET.

## Mechanical constraints this board is under

It is pinned by two things at once, which is what makes it harder than its
schematic suggests:

1. **The header fixes its position relative to the host board**, in all three
   axes.
2. **The buttons must reach the enclosure top** through their caps.

So the enclosure's top surface, the host board's mounting, and this board's
thickness and switch height are one dimension chain, not three independent
choices. Fix the host board and the enclosure top first; this board's stack
height is then whatever is left, and the caps absorb the remainder.

## Getting geometry out of CAD and into KiCad

Two DXFs, same names and same rules as the carrier board, because the
constraints are KiCad's rather than this project's:

| File | Imported to | Contains |
|---|---|---|
| `Outline.dxf` | `Edge.Cuts` | the board outline and any real cutouts |
| `Holes.dxf` | a user layer, e.g. `Dwgs.User` | every drilled position, as a placement template |

**The split is required, not tidiness.** KiCad's Import Graphics puts every
entity in a file onto one layer, so a combined file sent to `Edge.Cuts` gives
you a board outline plus a crowd of circular cutouts - a board that falls to
pieces on the router.

**Imported DXF circles are drawings. They drill nothing.** Real holes come from
footprints; `Holes.dxf` only says where to put them. Import it to a user layer,
place the footprints on the circles, then the layer has done its job.

**Only `Edge.Cuts` cuts.** Geometry on a user layer is inert - it does not cut,
does not drill, and does not reach the fab outputs - so reference geometry that
must not become a feature belongs in `Holes.dxf`, never in `Outline.dxf`. That
is how the header's position gets shown without the router taking it seriously.

**Mark pin 1 unambiguously in the DXF.** A plain rectangle, or a row of
identical circles, can be placed rotated 180 degrees - the same failure the
keying section above warns about, except it happens during layout instead of at
the bench, so the boards come back wrong rather than being caught. A
different-diameter circle, or a small cross offset to one corner, settles it.

Then **place the real footprint on the reference and stop trusting the DXF**.
The imported geometry is a placement target, not the connector: the footprint
carries the pads, and DRC only checks the footprint. Once it is placed, the user
layer can be hidden.

**Generate `Outline.dxf` from `Holes.dxf`** by deleting every circle except any
that are genuine cutouts, rather than drawing the two separately. Exporting them
independently once produced a 180 degree frame flip that would have put every
hole at the wrong end of the board. Same frame, same origin, one source.

What `Holes.dxf` has to carry for this board:

- the **header position**, which is fixed by the host board and is the datum
  everything else hangs off
- the **three switch centres**, fixed by the enclosure top and its cap bores
- **mounting holes**, and the asymmetry that stops the board mating rotated
- **test point positions**, if you want them placed rather than found

Keep the drill sizes to a small set and say what they are. On the carrier board
there were exactly four, which made every footprint checkable by eye - anything
outside those four groups was a mistake.

## Bring-up order

Each step separates a failure the one above it would mask.

1. **Bare board, no host.** Continuity from every header pin to where it should
   land. Confirm no 3V3 to GND short before anything is mated.
2. **Mated, host powered, nothing else fitted.** Confirm 3V3 at the encoder
   connector and at the test point. This is the step that catches a header
   mated one column out.
3. **Buttons.** Meter each line to GND, pressed and released, before trusting
   software. Then run the pendant and press each one - the log names the action.
4. **Encoder.**

   ```bash
   python tools/on_board.py micropython/pendant/probe_encoder.py
   python tools/on_board.py micropython/pendant/monitor_encoder.py
   ```

   The first gives levels and per-channel duty; the second gives decode quality
   over thirty seconds. **`errors` proves nothing on this board** - PCNT counts
   pulses and validates nothing, so it reads zero whatever happens. The two
   signals that carry information are the scale check and the duty split. A
   lopsided duty means one channel is barely transitioning, which is what a bad
   joint looks like, and is how one was found during the 3V3 work.
5. **Whole thing.** `python tools/run_pendant.py`, with the caps and enclosure
   fitted, wearing the gloves it will actually be used in. The button feel is
   the thing that cannot be measured from a bench.
