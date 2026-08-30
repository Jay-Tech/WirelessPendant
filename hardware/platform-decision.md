# Platform and link

The pendant moves to an ESP32-S3 all-in-one board with a dedicated ESP-NOW link
to a USB-attached receiver. This reverses the decision in
[carrier-board.md](carrier-board.md), and the reason is worth setting down,
because the earlier reasoning was not wrong - it was answering a different
question.

## The requirement that changed

Everything before this was tuned around one shop: which channel was quiet, where
the access point sat, whether the sender could reach Ethernet. All of it
sensible, and all of it beside the point.

**An open-source pendant cannot require the builder to have usable WiFi.** Plenty
of shops are detached, steel-clad, or have no coverage at all. "Works if your
network is good enough" is not a specification anyone else can satisfy, and it
turns every reproduction into a networking support thread.

That single requirement rules out the whole approach the earlier documents
assume - joining the user's network and finding the sender on it - regardless of
how well it measured here.

## What it rules out, and what it leaves

The pendant needs a link it brings with it. Three ways to get one:

| | Pendant hardware | Encoder | Radio work |
|---|---|---|---|
| Pico W pair, receiver as SoftAP | the carrier board | hard IRQ, working | none, reuses the stack |
| RP2350 all-in-one + nRF24 | prebuilt | hard IRQ, working | own the link layer |
| **ESP32 all-in-one + ESP-NOW** | **prebuilt** | **PCNT, tested** | **turnkey** |

nRF24 loses on the same ground that decided everything else. Its modules are a
market lottery - counterfeit silicon, unmatched antennas, missing decoupling,
and folklore that begins "solder a capacitor across the power pins". Tolerable
on one bench, poison for a project whose point is that a stranger can build it.

The Pico W pair is the smallest change and would have been the right first
experiment under the old framing. It loses because it cannot reach the prebuilt
hardware: the RP2350 all-in-one board has no radio at all, so a WiFi link means
building the carrier board anyway.

## Why the all-in-one board decides it

The BOM is the argument:

| Part |
|---|
| ESP32-S3-Touch-LCD-3.5 |
| MPG handwheel |
| 3 x panel-mount buttons |
| LiPo cell |
| Enclosure |

**No discrete parts at all.** The 5 V boost module and the 22 kOhm pair were on
this list until 2026-08-15; both are gone, measured out rather than reasoned
out - see "The encoder runs at 3V3" below. Five orderable items, no passives,
which is a stronger version of the argument this section is making.

No carrier PCB, no charger module, no power-path IC, no battery sense divider,
no power button wiring, no display connector. Someone can order that list and
start. The Pico route needs them to fab a board first, which is a different kind
of project.

Two things on that board that the Pico cannot match at all:

- **An IPEX connector for an external antenna**, via resoldering one resistor.
  For a handheld carried around a steel machine this is worth more than any
  latency figure measured here - the Pico 2 W's antenna is fixed.
- **AXP2101** doing charging, power path, fuel gauge and the power button, with
  a switching charger rather than the linear one the Amigo Pro uses.

It also carries an audio codec and speaker header, which makes an audible probe
complete or alarm possible on a tool used while watching the cut rather than the
screen, and 8 MB PSRAM, enough for a full 320x480x2 framebuffer.

## Already settled

- **PCNT decoding is proven.** `quadrature_pcnt.py` passed on hardware first
  run - direction, scale, lossless `take()`, the wrap handler, and every speed
  row clean to 62,863 edges/s against the 2,000 a hand produces. This was the
  risk that looked largest and it is retired.
- **The display driver transfers.** The board carries the same ST7796S and
  FT6336U panel already ordered for the Pico, so `st7796.py` and `touch.py`
  apply unchanged.
- **The power subsystem is the board's problem now.** Charger, power path,
  protection, fuel gauge and on/off button are all on it.
- **5 V is USB only.** The schematic has three power nets - VBUS, VBAT, VSYS -
  and one power IC, the AXP2101, whose outputs all step down. There is no net
  called 5V and no boost anywhere. This was read as "the encoder still needs a
  small 3V3 to 5 V boost, about 30 mA"; it turned out the encoder did not need
  5 V at all. See below.

## The encoder runs at 3V3, so there is no boost

Measured 2026-08-15. The encoder runs from the board's regulated 3V3 with
**both 22 kOhm removed**, and that is better than the arrangement it replaces
rather than merely cheaper.

**The interface was never the problem.** This wheel is open-collector with an
internal pull-up, so its output level is whatever its own supply is. On 3V3 with
nothing external it presents a clean 3.3 V high straight to the GPIO. The old
5 V arrangement put 5 V through a 22 kOhm to ground and landed at **3.06 V** -
so the "under-driven encoder" the README warned about describes the *previous*
wiring more than this one. Against the ESP32-S3's ~2.48 V input-high threshold,
3.3 V has the better margin.

**The resistors have to come out with the supply change, not after it.** Keeping
them at 3V3 divides against the internal pull-up and gives about **2.06 V**,
under the threshold on both the RP2350 (~2.15 V) and the S3 (~2.48 V). That
fails intermittently, which is the worst way for it to fail.

**Do not take encoder Vcc from the battery rail.** Because the level follows the
supply, VBAT puts 4.2 V on a pin whose absolute maximum is about 3.6 V - and it
sags to ~3.2 V as the cell drains, so the level moves with the state of charge.
The regulated rail holds it still for the whole discharge. It is a buck from the
cell, so it can droop slightly near cutoff, but 3.2 V worst case against a
2.48 V threshold is not a margin worth engineering around.

**Evidence.** `monitor_encoder.py`, 30 s at 3V3: **4,173 detents, 16,695
counts**, sustained 100-195 detents/s, peak 195.5, no dropouts and no rate
collapses, three counts unaccounted at the end which is where the wheel stopped.
`probe_encoder.py`: low 0.12 V, high >= 3.22 V, duty **48/52 and 49/51**.

Note that `errors` proves nothing on this board - PCNT counts pulses and
validates nothing, so it reads zero whatever happens. The two signals that do
carry information are the scale check and the per-channel duty; a lopsided duty
is how a bad joint on one channel was found during this work.

## Measured

ESP-NOW between the ESP32-S3 board and an ESP-WROOM-32, on the bench, against
the TCP link measured the same way - same 200 samples at 50 ms, same
percentiles. See `selftest_espnow.py` and `selftest_latency.py`.

| | ESP-NOW | TCP | |
|---|---|---|---|
| min | 9.36 | 8 | same |
| **median** | **9.76** | 61 | **6.3x** |
| mean | 15.02 | - | |
| p90 | 19.82 | 90 | 4.5x |
| **p99** | **59.77** | 207 | **3.5x** |
| max | 60.12 | 217 | 3.6x |

200/200, nothing unacknowledged, nothing silent.

**The tail is the result.** The test was written to distinguish two
explanations: if the median improved but p99 did not, the ceiling would be the
2.4 GHz environment and leaving the infrastructure behind would buy
determinism without buying latency. The p99 improved 3.5x, so the ceiling was
the network stack - association, beacons, access point scheduling and the
sender's own WiFi hop - and removing the infrastructure removed them.

The other tell is that **median sits on top of min**, 9.76 against 9.36. Almost
every sample is at the floor, so the link adds no queuing delay of its own.
Under TCP a median of 61 against a min of 8 meant nearly every packet paid
overhead.

In the units that matter: 9.8 ms is **0.41 mm** at 2500 mm/min, against a lag
budget of 13-17 mm. Transport has stopped being a term in the equation.

Bench figures, not shop figures, and both ends are MicroPython - C would be
quicker again.

### What the shop said, and why it changes the argument

The TCP column above is a **bench** figure. Measured again at the machine, over
a dedicated 2.4 GHz SSID, with the pendant on it and the sender's PC on a
different 5 GHz SSID - two wireless hops and a routing step:

| | bench TCP | shop TCP |
|---|---|---|
| min | 8 | 16 |
| median | 61 | 68 |
| p90 | 90 | **86** |
| p99 | 207 | **173** |
| max | 217 | 222 |

200/200 received, zero lost, one session. **The tail is better than the bench**,
across a topology chosen to be pessimistic.

That was measured while chasing motion that stumbled and would not recover, and
it is the reason the whole line of enquiry turned around. The faults were all
found elsewhere, each ruled in or out by measurement:

- the **DRO redraw** blocking the pendant's event loop for up to 82 ms, four
  jog ticks, invisible because the watchdog threshold was 100 ms
- the **planner depth target**, fitted for a 5000 mm/min machine and too
  shallow once `$110` reached 15000
- a **run-ahead bound** that could stop the planner filling but never restart
  it, so lag grew to 521 mm and stayed
- an **orphaned sender process** holding the port with no window

None of them transport. The same jog stream replayed from the PC over loopback
- `tools/replay_pendant.py`, no radio in the path - reproduced the fault exactly
when the pendant did and ran smooth when it did not.

**So the honest case for ESP-NOW is not latency.** 68 ms median with a 173 ms
p99 was never what made the pendant stumble, and a link seven times quicker
would not have fixed any of the four faults above. The 6.3x is real and it is
worth having, but it buys headroom rather than solving a problem.

The argument that survives intact is the one at the top of this document: an
open-source pendant cannot require the builder to have usable WiFi. That is a
**reproducibility** argument, not a performance one. This shop's network turned
out to be fine; the point is that the next builder's need not be, and no amount
of tuning here can make that true for them.

Worth stating plainly because the numbers above are seductive: anyone reading
this later - including whoever wrote it - should not conclude that ESP-NOW
fixed a latency problem. It did not have one to fix.

### Then ESP-NOW ran at the machine, and it holds

The whole chain, moving a real axis: pendant to receiver over ESP-NOW,
receiver on `main.py` presenting a serial port, `tools/espnow_bridge.py`
carrying that to the port the sender already listens on. The sender itself is
unmodified - it still thinks it is talking to a TCP pendant - which is the
point of doing it in that order.

**Feel is at parity with WiFi**, judged back to back on the same machine in the
same session, with run-off perceived as slightly *less* over ESP-NOW. The
numbers agree that nothing much changed:

| | WiFi | ESP-NOW |
|---|---|---|
| 0.5 mm peak lag | 77.6 mm | 83.5 mm |
| 1.0 mm peak lag | 112.7 mm | 96.0 mm |
| planner depth reached | 10-12 | 10 |

Equivalent, and that is the expected result once the mechanism is understood:
**lag is set by the run-ahead bound, not by the transport.** The fill runs up
to whatever the bound allows, so lag lands near the bound however quick the
link is. A faster transport does not lower it.

That leaves the reproducibility argument as the whole case for ESP-NOW, exactly
as the section above concluded - now with the reassurance that adopting it
costs nothing in feel.

### ESP-NOW measured in the shop, at last

Every ESP-NOW figure above was taken on a bench. Repeated at the machine, at
two positions, with the responder's own counts as the check:

| | bench | shop, near | shop, far |
|---|---|---|---|
| median | 9.76 | 9.86 | 9.86 |
| p90 | 19.82 | 29.63 | 29.84 |
| p99 | 59.77 | 79.97 | 59.78 |
| **max** | **60.12** | **139.39** | **89.84** |

Four replies unacknowledged across roughly 400 - about **1% loss** - and the
medians are indistinguishable from the bench. The link is good in the shop.

**The tail is not.** The maximum is twice the bench figure, and that is the
number that matters, for a reason worth stating plainly because it inverts the
headline comparison at the top of this document:

- **median**: ESP-NOW 9.86 against TCP's 68, about **7x** better
- **maximum**: ESP-NOW 139 against TCP's 222, about **1.6x** better

The 7x is what makes ESP-NOW look compelling. The 1.6x is what it can actually
be spent on, because everything downstream is sized against the worst case
rather than the typical one.

That resolves the failed bound. It has to clear planner depth plus a worst-case
round trip, and depth is a fixed 0.24 s:

    bench maximum:  0.24 + 0.060 = 0.30 s   ->  0.35 clears it
    shop maximum:   0.24 + 0.139 = 0.38 s   ->  0.35 cuts into it

Sized from the wrong maximum. Not RF loss, not a blocking radio call, both of
which were confidently diagnosed and wrong - just a bench number used where a
shop number was required. **~0.45 is what the measurement supports**, worth
about 10% of the run-off rather than the 30% predicted from bench figures.

**0.45 was then tried at the machine, and the bound stays at 0.5.** The numbers
moved as predicted - peak lag 102 mm down to 88, about 14% - and none of it was
worth having: the operator reported 0.5 as the smoothest of the two with no
noticeable difference in run-off. Reverted, and the per-transport mechanism
with it, since there is now no second value to carry.

So the run-ahead bound cannot be moved by changing transport, and that is the
final word on it. The reason is in the table above: the bound is sized by the
worst case, ESP-NOW's worst case is 139 ms against WiFi's 222, and 83 ms of
headroom on a 0.24 s depth is not enough to matter. **ESP-NOW's value is
reproducibility. It is not a performance change, and three separate attempts
to spend it as one have now failed** - once on a bench extrapolation, once on
a wrong maximum, and once at a value the measurement did support.

One caution for anyone re-running this. The first attempt reported 108/200
received and 90 "acknowledged but never answered", which read as catastrophic
asymmetric loss and produced two hardware conclusions before the responder's
own terminal contradicted both. The pinger matched replies against a sequence
byte and never resynchronised, so a single late answer put the stream
permanently off by one. Fixed, and late replies are now counted separately -
but the general lesson stands: **the responder's count is the check on the
pinger's, and only both terminals together can tell a late packet from a lost
one.**

### The loop stalls are the backpressure. Do not remove them

Over ESP-NOW the pendant reports around **twelve loop stalls a session, worst
case 100 ms**, where WiFi reports one. That looked like a defect for a while.
It is not, and this section exists so nobody tries to fix it again.

Timing the sends found the cause immediately:

    stall=1/38ms    tx=35/56ms
    stall=12/100ms  tx=132/104ms

`tx` counts sends taking half a jog tick or longer. 132 of them, worst 104 ms,
against a worst stall of 100 ms - the same event. `ESPNow.send()` is
synchronous: it waits for the peer's radio to acknowledge, inside the event
loop, so the jog tick waits with it.

**Removing the wait removed the stalls and made the pendant unusable.** With
`sync=False` the `tx` counter stopped climbing after the first sample and the
stall count froze, exactly as intended - and motion fell apart: planner depth
0 of 5, lag pinned at 98.9 mm and never recovering, 67% of the operator's
detents discarded. Rough at every step size.

Because the blocking send *is the flow control*. It paces the pendant to what
the radio can actually carry. Without it, messages go out faster than the link
delivers them, whatever fails to arrive is distance the machine never travels
while the pendant has already counted it, and that gap becomes permanent lag -
which pins the run-ahead bound, starves the planner, and discards two thirds of
the input.

So a 104 ms send is the radio applying backpressure, and the loop stalling is
how the pendant obeys it. The stall counter is measuring a working mechanism.

Worth recording the shape of the mistake as well as the finding. Five changes
were made to this transport chasing better numbers - a hysteresis band, three
values of the run-ahead bound, and this - and all five were reverted after the
machine disagreed. Every one optimised against something measurable: stall
counts, latency percentiles, depth-plus-round-trip arithmetic. The measurements
were sound each time; the inference from measurement to feel was not. **On this
subsystem the operator's report is the ground truth and the counters are
supporting evidence, not the other way round.**

**PSRAM costs nothing.** Repeated on the `SPIRAM_OCT` build, where MicroPython's
heap lives in external RAM rather than internal SRAM and could plausibly have
shown up as latency: min 9.81, median 9.92, p90 19.92, p99 39.90, again 200/200.
The median moved 0.16 ms, which is noise. The tail reads better but two runs
cannot separate that from run to run variation, so the claim is only that
nothing was lost.

That matters because the framebuffer headroom depends on it - `gc.mem_free()`
reports 8.3 MB against 226 KB on the plain build, and a 320x480x2 buffer needs
307 KB. So a full-frame update is affordable without paying for it in latency.

## The board's own pin map

Not in Waveshare's wiki or readable from the schematic PDF - taken from their
demo code at github.com/waveshareteam/ESP32-S3-Touch-LCD-3.5, examples 08 and
11, which agree.

| Signal | GPIO |
|---|---|
| LCD MOSI | 1 |
| LCD MISO | 2 |
| LCD DC | 3 |
| LCD SCLK | 5 |
| LCD backlight | 6 |
| **LCD CS** | **none** |
| **LCD RST** | **TCA9554 at 0x20, pin 1** |
| I2C SDA / SCL | 8 / 7 |
| FT6336U touch | 0x38 on that I2C, polled - no INT or RST pin |

An I2C scan confirms it: 0x18 ES8311, **0x20 TCA9554**, 0x34 AXP2101, 0x38
FT6336U, 0x51 PCF85063, 0x6b QMI8658. The TCA9554 is not in the board's
advertised feature list and answers writes but not reads, so a scan alone only
tells you something is there.

Touch is a drop-in: chip id 0xA3 reads 0x64 and vendor 0xA8 reads 0x11, exactly
what `touch.py` already checks. Only the pin numbers change, and INT and RST can
be dropped.

**`st7796.py` needs two changes**, neither large:

- **No chip select.** The panel is permanently selected, so the driver has to
  tolerate `cs=None` rather than toggling a pin that does not exist.
- **Reset is an I2C write**, not a pin. Waveshare's sequence is TCA pin 1 high,
  10 ms, low, 10 ms, high, 200 ms. Cleanest fix is for the driver to take a
  reset *callable* instead of a pin, so the caller supplies either a pin toggle
  or an expander write and the driver stays platform-agnostic.

Everything else - the init sequence, MADCTL, INVON, the framebuffer push - is
unchanged, because it is the same ST7796S behind it.

## Display: verified, and it cost nothing

Brought up on the board with the two driver changes above and nothing else.
`ROTATION = 0`, 320 x 480, red green and blue correct at the first attempt, and
a four-corner test lands where it should with the USB-C port at the bottom.

So the MADCTL value and `INVON` worked out for the other vendor's module are
right for this one too - the panel is oriented the same way. `st7796.py` and the
whole of `screen.py` transfer intact, including the portrait layout, the DRO
digits, the step grid and the probe page. None of it needed touching.

USB-C at the bottom also matches the reference pendant this is modelled on,
where the cable exits the bottom edge, so it suits the enclosure rather than
fighting it.

## Still open

- **Meter pin 2 on battery.** The schematic says USB only; some PMICs in this
  family have an OTG boost a parts list would not reveal. Ten seconds, once a
  battery exists.
- ~~**The encoder against the real wheel.**~~ Done. On GPIO9 and GPIO10 with the
  22 kOhm dividers and 5 V from header pin 2, one aligned revolution reads
  **exactly 400 counts** - 100 detents, full 4x decode, nothing lost. Reversals
  tracked cleanly over several thousand counts at around 185 detents/s.

  That number is the diagnostic, not a formality. PCNT cannot report illegal
  transitions the way `quadrature.py` does, so counts per revolution is what
  replaces the `errors` counter: an exact multiple of 400 says every edge was
  seen, and anything not divisible by four, or drifting across repeated turns,
  is the tell. Worth re-running once it is in the enclosure on a longer cable.

  An earlier attempt read 268, which was a turn judged by eye rather than a
  decoder fault - align the dial before trusting a single revolution.
- ~~**ESP-NOW at the machine.**~~ Done as far as *feel* goes: it drives the
  machine, at parity with WiFi, with slightly less run-off. See above.

  ~~**What is still unmeasured is its latency there.**~~ Measured: medians
  matching the bench, about 1% loss, and a tail twice as long as the bench's.
  See above. That accounts for the run-ahead bound that failed, and it caps how
  much the quicker link can buy at roughly 10% of the run-off.

  Still open, and unaffected by any of the above: whether the IPEX
  external antenna earns its place. On WiFi at the machine the board reported
  **-37 to -62 dBm** depending on where it was held, with zero lost packets at
  either end of that range - comfortable, and suggesting the internal antenna
  is adequate here. But that is a different protocol on a different band plan,
  and says nothing about a shop with more steel in the path.
- **Pairing.** ESP-NOW addresses by MAC. Needs a story for someone who owns two.
- ~~**Sender transport.**~~ Done. `PendantService` reads the receiver's serial
  port as well as listening on TCP, through an `IPendantChannel` that made the
  serial implementation an addition beside the TCP one rather than a change to
  it. The JSON-lines protocol did not change at all. Both transports stay, one
  pendant active at a time, newest wins - claimed by the accept on TCP and by
  the `hello` on serial, since the receiver's port is open whether or not a
  pendant is switched on.

  `tools/espnow_bridge.py` is retired by this and must not be left running: the
  port is exclusive, so it and the sender cannot both hold it.

  Verified against a receiver end to end - port opened and held, the start-up
  `rx_note` parsed without faulting, clean shutdown. **Not** yet driven by a
  pendant over the radio, which is the half that remains.
- **Registering a board should be one command.** Setting up a receiver is
  currently: run `tools/board.py`, read a serial number off the screen, paste it
  into `BOARDS`, then `sync_board.py --receiver`. The paste is the only manual
  step in an otherwise scripted process, and it is the one most likely to be got
  wrong - the ids are sixteen hex digits and the two ESP32-S3s enumerate
  identically, so a slip puts the receiver's firmware on the pendant.

  A `--register <name>` writing the attached board's id straight into `BOARDS`
  would collapse those two steps into one. It would also retire two notes that
  exist only because the step is manual: the "every id in BOARDS is this
  bench's" warning in `board.py`, and the matching first-time paragraph in the
  README.

  Deliberately not built yet. Every id in `BOARDS` is still this bench's, and
  what defaults to ship is not knowable until the hardware is settled -
  automating the paste before then automates a shape that is still moving.
- **Broadcast the jog messages.** The most promising untried idea on this link.

  ESP-NOW unicast is acknowledged and retried at the 802.11 MAC layer, and that
  is not optional - it is what makes a send take up to 104 ms and stall the
  loop. Declining to *wait* for it, with `sync=False`, does not help: the
  retries still happen, and the pendant simply gets ahead of the radio until
  the driver queue overflows silently. That was measured and reverted.

  Broadcast frames are neither acknowledged nor retried. That removes the cost
  rather than hiding it, and this protocol is unusually well suited to losing
  packets: `protocol.py` sends a detent count and a step size rather than a
  target position, precisely "so a dropped message loses a little motion
  instead of desynchronising a position the two ends would then disagree
  about". At the ~1% loss measured here, a lost jog is 0.1-1 mm the operator
  corrects with the wheel without noticing - which already happens whenever the
  emission cap discards a detent.

  Keep `hello` and `ping` unicast so discovery and pairing are unchanged. The
  receiver already registers a broadcast peer, so it would hear them today.

  What to watch: this removes the flow control the blocking send currently
  provides, exactly as `sync=False` did. The difference is that it also removes
  what that flow control existed to absorb, since with no retries the
  per-packet cost is constant and small. That is the assumption to test rather
  than to trust.
- **Coarse-step block quantisation.** A hypothesis with arithmetic behind it and
  no measurement yet.

  The operator reports minor bumps at 1.0 mm that the stall counter never sees,
  while 0.1 mm is smooth. The pendant can only send whole detents, and per tick
  it wants to send what the commanded feed drains:

      0.1 mm at F2500   ->  0.83 mm per 20 ms tick  ->  8.3 detents
      1.0 mm at F10000  ->  3.33 mm per 20 ms tick  ->  3.3 detents

  Rounding 8.3 is a 12% swing in block length. Rounding 3.3 is **33%**, and the
  surplus is discarded rather than banked, so the depth regulator picks 3 or 4
  tick by tick. `jog.py` already warns what unequal blocks feel like: "blocks of
  unequal length take unequal time at a fixed feed, so a short one landing on a
  shallow planner is a stumble".

  Confirming it means seeing block lengths alternate 3, 3, 4, 3. The trace
  tables used to show that and are off for machine work because printing blocks
  the loop, so it needs a cheaper instrument - the spread of emitted detent
  counts in the periodic one-liner would do.

  If it holds, the options are carrying the fractional residual so the average
  comes out right, or accepting that 1.0 mm at a 20 ms tick has inherently
  coarse blocks.

## What carries over

Most of it. The transport changes; almost nothing else does.

- **Every jog constant**, in the sense that none of them is a network property.
  `MIN_RUNAHEAD_MM`, the planner targets, the feed tables and the turn-rate
  window are machine and planner physics, and a change of transport does not
  touch them.

  What was written here before - that they carried margin because they were
  fitted on a shared network - was wrong, and worth leaving corrected rather
  than deleted. They were fitted against a **5000 mm/min** machine. Once `$110`
  reached 15000 several of them were badly off, and the coarse steps were
  unusable until they were refitted at the machine: `PLANNER_TARGET_BLOCKS` 6 to
  12, `STEP_MAX_FEED` trimmed to 8000 and 10000 at the coarse steps, and
  `RUNAHEAD_LIMIT_S` tried at 0.35, 0.4 and 1.0 before settling back at 0.5.

  The lesson generalises past this project: these constants are fitted to a
  *specific* machine's `$110` and `$120`, and anyone reproducing this on
  different hardware should expect to refit them. The figure that makes that
  tractable is planner depth, now reported in the pendant's periodic one-liner -
  without it, a target never being reached and a target set too low look
  identical.
- **The enclosure work.** The board outline changes; every constraint solved in
  CAD does not - encoder body depth driving the case, the 60 mm dial overhanging
  the board, buttons squeezed between display and dial, the wire pass-through,
  the force distributor and its ring rather than a boss.
- **The encoder wiring.** 5 V line driver, 22 kOhm dividers, the same argument
  against running it at 3V3.
- **`quadrature.py` and the hard-IRQ path**, which stay in the tree. If ESP-NOW
  disappoints, the Pico W pair is still there and still works.

## The receiver stays a separate device

Tempting to fold it into the PICOGPIO controller, which is already a
host-attached microcontroller on USB CDC. It should not be:

- That protocol's core rule is that the device **never sends unsolicited output**
  other than its banner, so a host can identify the port. A pendant stream is
  continuous and unprompted at up to fifty messages a second, which breaks the
  rule immediately.
- GPIO drives vacuum, lights and coolant. Those should not stop working because
  the radio firmware wedged.
- Two different watchdogs with two different meanings on one event loop.
- They want to be in different places - the GPIO board near the relays, the
  receiver where its antenna can see the operator.
