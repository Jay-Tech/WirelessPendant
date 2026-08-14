"""The pendant itself: handwheel -> jog messages -> sender.

Wires together the pieces that were built and tested separately - the
quadrature decoder, the jog scheduler, and the network link - and runs them as
concurrent tasks.

    python tools/on_board.py micropython/pendant/pendant.py

Needs `secrets.py` on the board with WIFI_SSID, WIFI_PASSWORD and SENDER_HOST.
Start the sender first (tools/mock_sender.py during development), then turn the
wheel and watch the DRO move.

To run on power-up, install this file as main.py alongside the modules:

    python tools/sync_board.py --main

Without --main the sync copies the modules only, so a development run cannot
quietly change what the board does when it is next switched on.

No display or buttons are required; both degrade to nothing if absent.

Note that there is no REPL available while this runs - `mpremote run` holds the
serial port for the whole session - so anything meant to be adjustable while
jogging has to be on a button, not a variable.
"""

import asyncio
import time

from machine import Pin

try:
    import link
    import protocol
    from buttons import ButtonPanel, PRESS, RELEASE, LONG_PRESS
    from jog import JogScheduler, TICK_MS
except ImportError:
    from pendant import link, protocol
    from pendant.buttons import ButtonPanel, PRESS, RELEASE, LONG_PRESS
    from pendant.jog import JogScheduler, TICK_MS

import secrets
import sys

# --- board profile --------------------------------------------------------
#
# Two boards, one firmware. Everything below this block is written against the
# names, so adding a third is a matter of another branch rather than edits
# scattered through the file.
#
# Keeping the Pico alive is not sentiment: it is the reference. When something
# on the ESP32 feels wrong, the only way to tell a platform problem from a port
# problem is to run the same code on hardware that already worked.
_ESP32 = sys.platform == "esp32"

if _ESP32:
    # Waveshare ESP32-S3-Touch-LCD-3.5. The display pins are not in Waveshare's
    # wiki and cannot be read off their schematic PDF - they come from their
    # demo code. See hardware/platform-decision.md.
    ENCODER_PIN_A, ENCODER_PIN_B = 9, 10
    BUTTON_PINS = (38, 39, 40)

    SPI_ID = 2
    PIN_SCK, PIN_MOSI, PIN_MISO = 5, 1, 2
    PIN_DC, PIN_BL = 3, 6
    # Two entries that are not GPIOs at all: chip select is tied low on this
    # board, and reset hangs off a TCA9554 expander. The display driver takes
    # None for the first and a callable for the second, so neither is a special
    # case here - see start_display().
    PIN_CS = None
    PIN_RST = None
    TCA_ADDRESS, TCA_LCD_RESET = 0x20, 1

    # Touch shares the board's I2C bus with the PMIC, RTC and IMU. No interrupt
    # or reset line is broken out, and none is needed: the controller is polled.
    PIN_TOUCH_SDA, PIN_TOUCH_SCL = 8, 7
    PIN_TOUCH_INT, PIN_TOUCH_RST = None, None
    TOUCH_I2C_ID = 0
else:
    # Raspberry Pi Pico 2 W.
    ENCODER_PIN_A, ENCODER_PIN_B = 2, 3
    BUTTON_PINS = (7, 8, 9)

    SPI_ID = 0
    PIN_SCK, PIN_MOSI, PIN_MISO = 18, 19, None
    PIN_DC, PIN_BL = 20, 22
    PIN_CS = 17
    PIN_RST = 21
    TCA_ADDRESS, TCA_LCD_RESET = None, None

    # Capacitive touch on its own bus. Level converted on the module, so 3.3V
    # logic is safe against it.
    PIN_TOUCH_SDA, PIN_TOUCH_SCL = 10, 11
    PIN_TOUCH_INT, PIN_TOUCH_RST = 12, 13
    TOUCH_I2C_ID = 1

# Which decoder, and it is not a preference. The RP2350 version registers its
# pin IRQ with hard=True, which the ESP32 port does not support at all, and a
# soft IRQ would sit behind the interpreter where a display refresh could hold
# it off long enough to drop edges. ESP32 counts in PCNT hardware instead, which
# nothing on the chip can delay. Both present the same class, so nothing below
# here knows which it got.
if _ESP32:
    try:
        from quadrature_pcnt import Quadrature
    except ImportError:
        from pendant.quadrature_pcnt import Quadrature
else:
    try:
        from quadrature import Quadrature
    except ImportError:
        from pendant.quadrature import Quadrature

START_AXIS = "X"
START_STEP_INDEX = 2  # 0.1 mm per detent

AXIS_ORDER = ("X", "Y", "Z")

# Wire the buttons you have; the rest idle high through their pull-ups and stay
# silent, so unpopulated entries cost nothing.
# Axis and step selection moved to the touch panel: the axis rows and the step
# grid are both larger targets than a button and, unlike a button, say what is
# selected without being read back off a status line. GP4-GP6 are free.
#
# What stays physical is what has to work without looking, and what has to work
# while a job is running - feed hold and cycle start are the only pendant
# commands the sender will forward mid-job, and hunting for a touch target is
# not something to do with a tool in the work. Zeroing stays on a long hold for
# the same reason it always was: it rewrites the work offset.
# Zeroing fires on a long hold, never a tap. It rewrites the work offset, and
# doing that by accident mid-job loses your datum. There is no manual jog cancel
# button: the scheduler already cancels when the wheel stops.
BUTTON_MAP = tuple(zip(BUTTON_PINS,
                       ("feed_hold", "cycle_start", "zero_axis")))

BUTTON_POLL_MS = 20

# Display. Optional throughout: if the panel is absent or miswired the pendant
# still jogs, which matters because the display is a convenience and the
# handwheel is the function. Its pins are in the board profile above.
DISPLAY_ENABLED = True

# Well inside a finger's dwell time, and cheap: the I2C read is a few
# hundred microseconds against the SPI the panel is already doing.
TOUCH_POLL_MS = 30

# How often the watchdog checks that the loop is still turning, and how late a
# wake-up has to be before it is worth reporting. 100 ms is five scheduler
# ticks - long enough that a busy tick does not trip it, short enough to catch
# a stall well before the link's 10 s deadline gives up.
# Zone kinds that respond to a touch-and-hold. Everything else selects on the
# tap and ignores a hold, so the progress bar stays hidden over them rather
# than promising a gesture that will not fire.
HOLD_TARGETS = ("probe",)

WATCHDOG_MS = 100

# Lateness that counts as a stall, in milliseconds.
#
# Was 100, which is the wrong size for what this is watching. The jog scheduler
# ticks every 20 ms, so a block only has to reach 40 ms to cost two ticks and
# make the next message a double-length one - and block length varying tick to
# tick is precisely the stumble the planner regulation exists to avoid. A
# threshold at 100 reports nothing until five ticks are gone.
#
# That gap mattered: a whole day of machine testing read "stall=0/0ms" as
# proof the loop was clean and looked elsewhere, when all it proved was that
# nothing blocked for a tenth of a second. Sized against TICK_MS now rather
# than against what would look alarming in a log.
STALL_WARN_MS = 30

# Stalls logged per session before it goes quiet. Counting continues either
# way and the periodic one-liner still reports the total and the worst.
#
# Bounded because the log line is written over USB CDC, and a host that is not
# draining makes print() block the loop - which is the fault this measures. An
# unbounded stall log at a 30 ms threshold is a diagnostic that manufactures
# its own findings, the same trap the jog trace tables fell into.
STALL_LOG_BUDGET = 3

# A step in the reported position larger than this is a discontinuity rather
# than motion, and invalidates the lag reference. See update_lag().
#
# Sized well clear of anything real: $110 is 15000, so 250 mm/s, and status
# arrives at 10 Hz - 25 mm between frames at absolute maximum feed. Double
# that again for a late frame and this still cannot fire on genuine travel,
# while catching a work offset change or a sender that has not yet learned
# where the machine is.
POSITION_JUMP_MM = 50.0

# Which panel is fitted. "st7796" is the MSP3525/MSP3526 3.5" 320x480 IPS;
# "ili9341" is the 2.4" 320x240 it replaced, kept because it is the fallback
# if the new one is ever out of the loop.
PANEL = "st7796"

# 0 is native portrait on the ST7796S, 320 wide by 480 tall, and is also how
# the FT6336U reports touch coordinates on this module - so keeping them
# matched means touch needs no swap or flip. The ILI9341 wanted 90 for
# landscape. Either way the layout follows the width and height the driver
# reports.
ROTATION = 0 if PANEL == "st7796" else 90
SPI_BAUD = 20_000_000

# How often the panel is repainted, or 0 to build the layout once and never
# touch it again.
#
# Zero is a diagnostic, not a mode anyone should jog in - the DRO freezes. It
# exists because the display is the one thing the pendant does that the PC-side
# replay does not, and the replay is smooth at a step and feed where the
# pendant stumbles. Redrawing the DRO digits is a blocking SPI write on the
# same event loop as the jog tick, so it is a candidate for the stumble that
# nothing else has ruled out, and switching it off is the way to find out in
# one run rather than reason about it.
DISPLAY_REFRESH_MS = 100

# Refresh interval while traversing, and the commanded feed that counts as
# traversing.
#
# Measured: with refresh off entirely the stumble at 0.1 mm went away and the
# coarse steps improved markedly, so the redraw was the largest single thing
# holding the loop. Off is not an option though - a pendant whose DRO freezes
# while it moves is worse than one that stumbles.
#
# Scaling by feed serves both cases instead of trading one for the other. Above
# a traverse feed the digits change faster than anyone can read them, so a
# fifth of the rate costs nothing that was being used; below it the operator is
# placing the tool and wants the numbers live - and a missed tick there is
# 0.1 mm rather than 3 mm, so the same block hurts proportionally less.
DISPLAY_REFRESH_TRAVERSE_MS = 500
DISPLAY_TRAVERSE_FEED = 2000.0

# A reported feed below this share of the commanded one, while the commanded
# feed is meaningful, counts as the machine failing to hold what it was asked
# for rather than simply moving slowly.
FEED_COLLAPSE_RATIO = 0.5
FEED_COLLAPSE_FLOOR = 500

# One dump per episode, not per status frame.
# Raised from 3 s. At three seconds a bad minute produced twenty dumps of
# twenty-six lines each, and the printing blocked the event loop for a full
# second. The budget in jog.py bounds the total; this bounds the rate, so the
# first few dumps are spread across the run rather than spent in the first ten
# seconds of it.
COLLAPSE_DUMP_QUIET_MS = 10000

STATUS_REPORT_S = 15

encoder = None
scheduler = None
pendant_link = None
screen = None
touch = None

state = {"dro": None, "machine_state": "?", "status_frames": 0,
         "lag_mm": 0.0, "peak_lag_mm": 0.0, "_ref": None,
         "lag_session": 0, "lag_last_pos": None, "frames_without_bf": 0,
         "lag_enabled": True, "actual_feed": 0, "feed_collapses": 0,
         "last_collapse_dump": -60000, "planner_free": 0,
         "planner_min": 999, "planner_max": 0, "bf_warned": False,
         "stalls": 0, "worst_stall_ms": 0, "hold_target": False}


def on_message(message):
    if message.get("t") != protocol.T_STATUS:
        return
    state["status_frames"] += 1
    wpos = message.get("wpos")
    state["dro"] = wpos
    state["machine_state"] = message.get("state", "?")

    # What the controller says it is actually running at. A commanded feed that
    # holds steady while this collapses to near zero and back is the planner
    # executing one block at a time and decelerating at the end of each - the
    # symptom the operator sees as 0 to 9000 and back, and the one thing no
    # amount of pendant-side instrumentation could ever show.
    # Free planner slots at the controller. The pendant models a queue it has
    # sent ahead, but until now had no way to check that model against the one
    # place it matters. A buffer that never fills means the lookahead the
    # scheduler assumes it is building does not exist.
    free = message.get("bf")
    if free is None:
        # Said once, when enough frames have arrived carrying no buffer field
        # at all to be sure none ever will.
        #
        # It matters because without Bf: the pendant silently runs its degraded
        # path: emission pinned at the drain rate, the planner a block or two
        # deep, and motion that ripples for a reason nothing on screen explains.
        #
        # This used to sit inside the branch below, where the field is present -
        # so it could only ever fire on a controller that does report Bf:, which
        # is the opposite of what it warns about. What it actually caught was a
        # zero, and zero is what the sender reports before it knows the
        # machine's state, so starting the pendant before the sender produced
        # the warning against a controller reporting Bf: perfectly. Judged on
        # absence now, which is the condition it describes.
        state["frames_without_bf"] += 1
        if not state["bf_warned"] and state["frames_without_bf"] > 50:
            state["bf_warned"] = True
            link.log("controller is not reporting Bf: - planner depth is")
            link.log("  unknown, so motion will be rougher. Enable the")
            link.log("  buffer-state bit in $10 (add 2) and restart.")
    elif scheduler is not None:
        state["planner_free"] = free
        scheduler.set_planner_free(free)
        if free:
            if free < state["planner_min"]:
                state["planner_min"] = free
            if free > state["planner_max"]:
                state["planner_max"] = free

    # The sender's probe state: which corner it will use, and whether a cycle
    # is already running. Shown on the probe page so a hold never fires at a
    # target only visible on a screen behind you.
    probe_state = message.get("probe")
    if probe_state is not None and screen is not None:
        try:
            screen.set_probe_state(probe_state.get("corner", "?"),
                                   bool(probe_state.get("busy")))
        except Exception:
            pass

    actual = message.get("fr")
    if actual is not None and scheduler is not None:
        state["actual_feed"] = actual
        scheduler.actual_feed = actual
        commanded = scheduler.feed
        # Only while the wheel is actually driving. A stop decays the
        # commanded feed over about a second while the machine runs out what
        # is queued, and reporting that as a collapse buried the real ones -
        # most of a log's worth of dumps were the operator letting go.
        if (scheduler.moving and commanded > FEED_COLLAPSE_FLOOR
                and actual < commanded * FEED_COLLAPSE_RATIO):
            state["feed_collapses"] += 1
            # Dump the trace at the moment the machine falls behind what it was
            # asked for, rate-limited so one episode is one dump. This is the
            # only view that shows both numbers on the same timeline.
            now = time.ticks_ms()
            if time.ticks_diff(now, state["last_collapse_dump"]) > COLLAPSE_DUMP_QUIET_MS:
                state["last_collapse_dump"] = now
                scheduler.dump_trace(
                    "feed collapse {}: commanded {:.0f}, actual {}, "
                    "planner free {}".format(
                        state["feed_collapses"], commanded, actual,
                        state["planner_free"]))

    # Everything below here is diagnostic. It runs inside the receive loop, so
    # anything it raises would kill the session and present as a link that will
    # not stay up - a measurement taking down the thing it measures. Nothing
    # here is worth losing the pendant over.
    try:
        update_lag(wpos)
    except Exception as exc:
        link.log("lag measurement failed, disabling it: {}: {}".format(
            type(exc).__name__, exc))
        state["lag_enabled"] = False


def update_lag(wpos):
    """Commanded distance less what the machine reports moving.

    The scheduler's queue is an open-loop model and cannot see the controller's
    planner or the sender's buffer, so a backlog accumulating in either is
    invisible to it. This is measured rather than estimated.
    """
    if not state["lag_enabled"] or wpos is None or scheduler is None:
        return
    if scheduler.axis not in AXIS_ORDER:
        return
    index = AXIS_ORDER.index(scheduler.axis)
    if index >= len(wpos):
        return

    # A reconnect invalidates the reference as surely as an axis change does.
    #
    # commanded_mm keeps counting while the link is down, and the motion behind
    # it is discarded rather than executed - link.py drops the queue on
    # reconnect on purpose, so a minute-old detent cannot fire late - so the
    # machine never travels that distance. Carried across the gap, the
    # difference reads as lag that is permanently present and never decays.
    #
    # Which pins the run-ahead bound, starves the planner and produces motion
    # that is rough and will not reach speed. Seen after restarting the sender:
    # only restarting the pendant cleared it, because that was the one thing
    # that reset this reference.
    # And so does the position jumping somewhere it cannot have travelled.
    #
    # This measures a difference between two running totals, so it is only
    # meaningful while both describe the same continuous motion. Anything that
    # moves the reported position without the pendant commanding it - a work
    # offset changing, homing, or the sender reporting before it knows where
    # the machine is - shifts one total and not the other, and the difference
    # is then a permanent offset that reads as lag and never decays.
    #
    # Measured: the pendant was started before the sender, and on the first
    # status it took its reference against a zeroed DRO - the sender emits
    # 0.000 before it has the real position, seen directly during an earlier
    # reconnect. The machine was at X+384, the pendant reported 390.5 mm of
    # lag against 12 mm of commanded travel, the bound pinned, and 92% of the
    # wheel's detents were discarded until it was restarted.
    #
    # Zeroing an axis does the same thing, and that is on this pendant's own
    # button: the work offset changes, wpos steps to zero, and a few hundred
    # millimetres of phantom lag arrive with it.
    #
    # No axis here travels POSITION_JUMP_MM between two status frames - $110 is
    # 15000, so 250 mm/s, and status arrives at 10 Hz, which is 25 mm flat out.
    # A larger step is a discontinuity rather than motion, and the reference
    # starts again from wherever the machine now says it is. A false positive
    # costs one re-reference; a false negative costs the session.
    previous = state["lag_last_pos"]
    jumped = (previous is not None
              and abs(wpos[index] - previous) > POSITION_JUMP_MM)
    state["lag_last_pos"] = wpos[index]

    session = pendant_link.stats["sessions"] if pendant_link else 0
    reference = state["_ref"]
    if (reference is None or reference[0] != scheduler.axis
            or session != state["lag_session"] or jumped):
        state["lag_session"] = session
        state["_ref"] = (scheduler.axis, wpos[index], scheduler.commanded_mm)
        # Cleared rather than left to be overwritten next frame: the scheduler
        # reads this every tick and would spend the interval bounding itself
        # against a figure already known to be meaningless.
        state["lag_mm"] = 0.0
        scheduler.lag_mm = 0.0
        return

    _, start_pos, start_cmd = reference
    lag = abs((scheduler.commanded_mm - start_cmd) - (wpos[index] - start_pos))
    state["lag_mm"] = lag
    # The scheduler bounds its own run-ahead against this. It is the only
    # closed-loop figure available for how far the machine is behind the hand:
    # planner depth says how much work is queued but not how much distance, and
    # at a coarse step those differ by an order of magnitude.
    scheduler.lag_mm = lag
    if lag > state["peak_lag_mm"]:
        state["peak_lag_mm"] = lag


def handle_button(action, event):
    """Map one button event onto pendant state or a message to the sender."""
    # Axis and step live entirely on the pendant: they change what future jog
    # messages say, and the sender has no opinion about them.
    if action == "zero_axis":
        if event == PRESS:
            link.log("hold to zero {}".format(scheduler.axis))
        elif event == LONG_PRESS:
            link.log("zeroing {}".format(scheduler.axis))
            return protocol.zero(scheduler.axis)
        return None

    # Machine controls are forwarded with their up/down state rather than as
    # one-shot events, so the sender can distinguish a held button from a tap.
    if action in ("feed_hold", "cycle_start") and event in (PRESS, RELEASE):
        down = event == PRESS
        link.log("{} {}".format(action, "down" if down else "up"))
        return protocol.button(action, down)

    if event == LONG_PRESS:
        link.log("long press on {} (no action bound)".format(action))

    return None


async def poll_buttons():
    panel = ButtonPanel(BUTTON_MAP)
    link.log("buttons on GP{}".format(
        "/GP".join(str(pin) for pin, _ in BUTTON_MAP)))
    while True:
        for action, event in panel.poll(time.ticks_ms()):
            message = handle_button(action, event)
            if message is not None and pendant_link is not None:
                pendant_link.send(message)
        await asyncio.sleep_ms(BUTTON_POLL_MS)


def start_touch():
    """Bring up the touch controller, or run without it.

    Failing soft for the same reason the display does: axis and step selection
    is a convenience, and a pendant that refuses to jog because a touch panel
    is unplugged is worse than one that jogs without selection. The log line
    says which, so a silently button-less pendant is not a mystery.
    """
    try:
        from touch import Touch
    except ImportError:
        try:
            from pendant.touch import Touch
        except ImportError as exc:
            link.log("touch module missing ({}) - selection unavailable".format(exc))
            return None

    try:
        panel = Touch(PIN_TOUCH_SDA, PIN_TOUCH_SCL, PIN_TOUCH_RST,
                      PIN_TOUCH_INT, TOUCH_I2C_ID)
    except Exception as exc:
        link.log("touch unavailable ({}: {})".format(type(exc).__name__, exc))
        return None

    if not panel.present:
        link.log("no touch controller on I2C{} (SDA GP{}, SCL GP{})".format(
            TOUCH_I2C_ID, PIN_TOUCH_SDA, PIN_TOUCH_SCL))
        link.log("  axis and step cannot be selected - check the 6P touch FPC")
        return panel

    link.log("touch FT6336U (id 0x{:02X}) on GP{}/GP{}".format(
        panel.chip_id, PIN_TOUCH_SDA, PIN_TOUCH_SCL))
    return panel


def start_display():
    """Bring up the panel, or return None and carry on without it."""
    if not DISPLAY_ENABLED:
        return None
    try:
        from machine import SPI
        try:
            from screen import DroScreen
            from st7796 import ST7796S
            from ili9341 import ILI9341
        except ImportError:
            from pendant.screen import DroScreen
            from pendant.st7796 import ST7796S
            from pendant.ili9341 import ILI9341

        if PIN_MISO is None:
            spi = SPI(SPI_ID, baudrate=SPI_BAUD, polarity=0, phase=0,
                      sck=Pin(PIN_SCK), mosi=Pin(PIN_MOSI))
        else:
            spi = SPI(SPI_ID, baudrate=SPI_BAUD, polarity=0, phase=0,
                      sck=Pin(PIN_SCK), mosi=Pin(PIN_MOSI),
                      miso=Pin(PIN_MISO))

        # Reset is a pin on one board and an I2C write on the other. The driver
        # accepts either - a pin number or a callable - so the expander stays
        # out of it and out of the driver.
        #
        # This opens the same bus start_touch() will open again a moment later.
        # Harmless: both ask for the same frequency, the expander is not touched
        # after reset, and sharing one object would mean threading it through
        # two constructors to save an initialisation that costs nothing.
        reset = PIN_RST
        if TCA_ADDRESS is not None:
            from machine import I2C
            try:
                from tca9554 import TCA9554
            except ImportError:
                from pendant.tca9554 import TCA9554
            expander = TCA9554(I2C(TOUCH_I2C_ID, sda=Pin(PIN_TOUCH_SDA),
                                   scl=Pin(PIN_TOUCH_SCL), freq=400000),
                               TCA_ADDRESS)
            reset = expander.reset_line(TCA_LCD_RESET)

        # The panel is built here and handed to the layout, so fitting a
        # different one is a change to these two lines rather than to the
        # layout - which is the whole reason the display is injected.
        driver = ST7796S if PANEL == "st7796" else ILI9341
        display = driver(spi, PIN_CS, PIN_DC, reset, PIN_BL, ROTATION)
        panel = DroScreen(display)
        panel.splash("connecting...")
        link.log("display on SPI{} at {} MHz".format(
            SPI_ID, SPI_BAUD // 1000000))
        return panel
    except Exception as exc:
        # A missing or miswired panel must not stop the pendant jogging.
        link.log("display unavailable ({}: {}) - running headless".format(
            type(exc).__name__, exc))
        return None


def handle_touch(kind, target, value):
    """Act on a tap or a hold that landed inside a zone.

    Taps select; holds do things. Selection is reversible and wants to feel
    immediate, so it fires on contact - while anything that drives the tool at
    the work waits for 800 ms of deliberate contact, the same as the physical
    zero button. One convention for "this one is consequential", rather than a
    different gesture depending on which surface the control lives on.
    """
    if kind == "tap":
        if target == "axis" and value != scheduler.axis:
            # Changing axis mid-motion has to flush what is queued, or the old
            # axis keeps running on after the operator has moved on.
            cancel = scheduler.set_axis(value)
            if cancel and pendant_link:
                pendant_link.send(cancel)
            link.log("axis -> {}".format(value))
        elif target == "step":
            scheduler.set_step(value)
            link.log("step -> {} mm".format(value))
        elif target == "page":
            # The DRO is page 0 and the one to come back to: a pendant left on
            # a menu does not show where the machine is.
            screen.show_page(0 if value == "dro" else 1)
        return

    if kind == "hold" and target == "probe":
        if pendant_link is None or not pendant_link.connected:
            link.log("probe {} ignored - no link to the sender".format(value))
            return
        # The pendant asks; the sender decides. It holds the parameters, knows
        # whether a job is running, and is the only side that can refuse for a
        # reason the operator would recognise.
        pendant_link.send(protocol.probe(value))
        link.log("probe {} requested".format(value))


async def watch_touch():
    """Turn taps into axis and step selections.

    Runs as its own task rather than inside the display loop, because the
    display redraws at 5 Hz and a selection that took up to 200 ms to register
    feels broken in the hand. Polling is 30 ms; the I2C read is a few hundred
    microseconds, so this costs nothing next to the SPI the panel is already
    doing.
    """
    if touch is None or screen is None or not touch.present:
        return

    while True:
        try:
            event = touch.poll()
            if event is not None:
                kind = event[0]
                if kind == "progress":
                    # Only where a hold means something. The bar filling is a
                    # promise that holding will do something, and on the DRO
                    # page it will not - selection there is a tap, because axis
                    # and step are reversible and waiting 800 ms to change one
                    # twice would be tedious. Showing the bar anyway invites
                    # the operator to wait for a gesture that page does not
                    # have, which is how it read as broken.
                    if screen is not None and state["hold_target"]:
                        screen.set_hold_progress(event[1])
                elif screen is not None:
                    screen.set_hold_progress(0.0)
                    hit = screen.zones.hit(event[1], event[2])
                    # Recorded on the press, because a progress event carries
                    # only a fraction - by then there is nothing to look up.
                    if kind == "tap":
                        state["hold_target"] = (hit is not None
                                                and hit[0] in HOLD_TARGETS)
                    if hit is not None:
                        handle_touch(kind, hit[0], hit[1])
        except Exception as exc:
            # A touch fault must not take the pendant down. The wheel and the
            # link are the parts that matter; selection is a convenience.
            link.log("touch error: {}: {}".format(type(exc).__name__, exc))
            await asyncio.sleep_ms(500)

        await asyncio.sleep_ms(TOUCH_POLL_MS)


async def refresh_display():
    """Push current state to the panel.

    Polls rather than being driven by events because the screen redraws only
    changed characters anyway - a poll that finds nothing different costs a few
    string comparisons, and this keeps display work off the message handler.
    """
    if screen is None:
        return

    # Replace the splash with the live layout now the panel is known good.
    screen.build()

    if not DISPLAY_REFRESH_MS:
        link.log("display refresh OFF - DRO will not update (diagnostic)")
        return

    while True:
        try:
            # Yield between fields. Each set_* is a blocking SPI write, and
            # run back to back they were one uninterrupted block long enough to
            # cost four jog ticks - measured at 82 ms, against a 20 ms tick.
            # Broken up, the scheduler can run between them, and the panel
            # takes the same total time to paint either way.
            #
            # This is the whole of it: with the refresh switched off the loop
            # reported stall=0/0ms for an entire session, where the same run
            # with it on reported fourteen.
            screen.set_link(pendant_link.connected if pendant_link else False)
            await asyncio.sleep_ms(0)
            screen.set_state(state["machine_state"])
            await asyncio.sleep_ms(0)
            screen.set_mode(scheduler.axis, scheduler.step,
                            scheduler.feed)
            await asyncio.sleep_ms(0)
            if state["dro"]:
                screen.set_position(state["dro"])
        except Exception as exc:
            link.log("display error: {}: {}".format(type(exc).__name__, exc))
            return
        # Slower while traversing, where the digits are a blur anyway, and
        # unchanged at the feeds where the operator is placing the tool.
        traversing = scheduler is not None and \
            scheduler.feed >= DISPLAY_TRAVERSE_FEED
        await asyncio.sleep_ms(DISPLAY_REFRESH_TRAVERSE_MS if traversing
                               else DISPLAY_REFRESH_MS)


async def publish_mode():
    """Tell the sender the selected axis and step whenever either changes.

    Also re-publishes on a new session, since a reconnected sender has no
    memory of what the pendant was set to - tracking the session count covers
    both cases with one comparison.
    """
    last = None
    while True:
        if pendant_link is not None and pendant_link.connected:
            current = (scheduler.axis, scheduler.step,
                       pendant_link.stats["sessions"])
            if current != last:
                last = current
                pendant_link.send(protocol.mode(
                    scheduler.axis, scheduler.step))
        await asyncio.sleep_ms(100)


async def status_led():
    """no wifi = fast blink, idle = heartbeat, connected = solid.

    Written only on change: on the Pico the LED is on the CYW43, so each write
    is an SPI transaction competing with the radio. See the serial bridge for
    the latency this cost when it was rewritten every cycle.

    Boards without one simply do without. The ESP32-S3 display board has no
    user LED - its two indicators are wired to the charger and the power rail,
    not to a GPIO - and on a pendant with a screen the LED was never the way
    you learn the link is down anyway.
    """
    try:
        led = Pin("LED", Pin.OUT)
    except (ValueError, TypeError):
        link.log("no status LED on this board - the screen says it instead")
        return

    lit = None

    def show(on):
        nonlocal lit
        if lit is not on:
            led.on() if on else led.off()
            lit = on

    while True:
        if pendant_link is None or not pendant_link.connected:
            show(not lit)
            await asyncio.sleep_ms(150)
        else:
            show(True)
            await asyncio.sleep_ms(200)


async def watchdog():
    """Measure the event loop's own scheduling latency.

    A blocked loop and a silent sender are indistinguishable from the link's
    point of view: both end as "no traffic for 10s", because the deadline task
    cannot run while the loop is blocked and fires the moment it resumes. This
    tells them apart by asking a task that does nothing how late it was woken.

    The likeliest way to block this loop is printing. Every trace dump is 25
    lines over USB CDC, and if the host is not draining that as fast as it
    arrives, print() blocks - taking the socket read down with it. A pendant
    that disconnects because it was too busy describing itself is worth being
    able to prove rather than suspect.
    """
    last = time.ticks_ms()
    while True:
        await asyncio.sleep_ms(WATCHDOG_MS)
        now = time.ticks_ms()
        late = time.ticks_diff(now, last) - WATCHDOG_MS
        last = now
        if late > STALL_WARN_MS:
            state["stalls"] += 1
            if late > state["worst_stall_ms"]:
                state["worst_stall_ms"] = late
            if state["stalls"] <= STALL_LOG_BUDGET:
                link.log("loop stalled {} ms - nothing ran, including the link"
                         .format(late))


async def report():
    """Periodic one-liner, so a headless pendant is not silent."""
    while True:
        await asyncio.sleep(STATUS_REPORT_S)
        dro = state["dro"]
        position = "  ".join(
            "{}{:+8.3f}".format(a, v)
            for a, v in zip(("X", "Y", "Z"), dro)) if dro else "no status yet"
        # Dropped detents are reported because the effect is otherwise
        # invisible: at a coarse step the wheel simply feels unresponsive, with
        # nothing to say the motion was discarded rather than never commanded.
        sent = scheduler.stats["detents"]
        dropped = scheduler.stats["dropped_detents"]
        kept = 100 * sent // (sent + dropped) if (sent + dropped) else 100
        # Sessions and stalls ride along because a reconnect is otherwise only
        # visible as two lines that scrolled past minutes ago, and the question
        # after one is always whether the link dropped or the loop stopped
        # servicing it.
        #
        # Planner depth is here because everything the scheduler does is aimed
        # at it and none of it was visible: the target says twelve blocks, and
        # a feed dropping from 9000 to 7146 says the chain behind it was worth
        # about two. Whether the target is wrong or simply never reached are
        # opposite faults with opposite fixes, and only this number tells them
        # apart. Held now and deepest held, against capacity - it was only ever
        # readable in the trace tables, which are off for machine work.
        capacity = scheduler.planner_capacity
        held = capacity - state["planner_free"] if capacity else 0
        deepest = capacity - state["planner_min"] if capacity else 0
        # Only the ESP-NOW link reports these, so the WiFi one-liner keeps its
        # existing shape. It answers one open question: that transport reports
        # around eleven loop stalls a session where WiFi reports one, and a
        # synchronous radio send waiting out its retries is the only candidate
        # left after RF loss and the run-ahead bound were both ruled out.
        tx = ""
        if pendant_link.stats.get("worst_tx_ms"):
            tx = " tx={}/{}ms".format(pendant_link.stats.get("slow_tx", 0),
                                      pendant_link.stats["worst_tx_ms"])
        link.log(
            "{} | axis {} step {} F{:.0f}/act{} collapse={} | {} | detents={}"
            " dropped={} ({}% kept) lag={:.1f}/{:.1f}mm depth={}/{} of {}"
            " err={} sess={} stall={}/{}ms{}".format(
                "up" if pendant_link.connected else "DOWN",
                scheduler.axis, scheduler.step, scheduler.feed,
                state["actual_feed"], state["feed_collapses"], position,
                sent, dropped, kept,
                state["lag_mm"], state["peak_lag_mm"],
                held, deepest, capacity,
                encoder.errors,
                pendant_link.stats["sessions"], state["stalls"],
                state["worst_stall_ms"], tx))


async def main():
    global encoder, scheduler, pendant_link, screen, touch

    print("\ngrblHAL wireless pendant")
    print("=" * 46)

    screen = start_display()
    touch = start_touch()
    encoder = Quadrature(ENCODER_PIN_A, ENCODER_PIN_B)
    link.log("handwheel on GP{}/GP{}".format(ENCODER_PIN_A, ENCODER_PIN_B))

    scheduler = JogScheduler(encoder, axis=START_AXIS,
                             step_index=START_STEP_INDEX)
    link.log("axis {} at {} mm/detent, {} Hz".format(
        scheduler.axis, scheduler.step, 1000 // TICK_MS))

    # Which transport, chosen in secrets.py so switching one at the machine is
    # a reboot rather than an edit. Both paths stay live deliberately: the WiFi
    # one works and every jog constant was fitted over it, so it is the
    # reference any ESP-NOW result gets compared against.
    if getattr(secrets, "USE_ESPNOW", False):
        try:
            import espnow_link
        except ImportError:
            from pendant import espnow_link

        # No host, no port, no network to join. The pendant broadcasts until a
        # receiver answers, so there is nothing here to get wrong in secrets.py
        # and nothing to change when the PC's address does.
        pendant_link = espnow_link.PendantLink(on_message=on_message)
        link.log("ESP-NOW - looking for a receiver")
    else:
        ip = link.wifi_connect(
            secrets.WIFI_SSID, secrets.WIFI_PASSWORD,
            getattr(secrets, "HOSTNAME", None))
        if ip is None:
            link.log("no network - check secrets.py")
            return

        host = getattr(secrets, "SENDER_HOST", None)
        if host is None:
            link.log("set SENDER_HOST in secrets.py to the sender's address")
            return
        port = getattr(secrets, "SENDER_PORT", 8422)

        pendant_link = link.PendantLink(host, port, on_message=on_message)
        link.log("sender at {}:{} - turn the wheel".format(host, port))

    await asyncio.gather(
        pendant_link.run(),
        scheduler.run(pendant_link),
        poll_buttons(),
        publish_mode(),
        refresh_display(),
        watch_touch(),
        watchdog(),
        status_led(),
        report(),
    )


try:
    asyncio.run(main())
except KeyboardInterrupt:
    print("\nstopped.")
finally:
    if encoder is not None:
        encoder.deinit()
    asyncio.new_event_loop()
