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
| 2 x 22 kOhm |
| 1 x small fixed 5 V boost module |
| LiPo cell |
| Enclosure |

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
  called 5V and no boost anywhere. So the encoder still needs a small 3V3 to 5 V
  boost, about 30 mA, exactly as sized for the carrier board.

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

## Still open
- **Meter pin 2 on battery.** The schematic says USB only; some PMICs in this
  family have an OTG boost a parts list would not reveal. Ten seconds.
- **Prototype the link** with the ESP-WROOM-32 devkit as receiver - ESP-NOW is
  interoperable across ESP32 families, so both ends are already in hand.
- **Pairing.** ESP-NOW addresses by MAC. Needs a story for someone who owns two.
- **Sender transport.** `PendantService` becomes a serial reader rather than a
  TCP listener. The JSON-lines protocol can stay as it is.

## What carries over

Most of it. The transport changes; almost nothing else does.

- **Every jog constant.** `MIN_RUNAHEAD_MM`, the planner targets, the feed
  tables and the turn-rate window are machine and planner physics, not network
  properties. They were also fitted on a *shared* network under real shop
  conditions, so they carry margin rather than sitting on the edge - a dedicated
  link can only shorten the path they were measured against.
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
