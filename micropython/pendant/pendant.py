"""The pendant itself: handwheel -> jog messages -> sender.

Wires together the pieces that were built and tested separately - the
quadrature decoder, the jog scheduler, and the network link - and runs them as
concurrent tasks.

    python tools/on_board.py micropython/pendant/pendant.py

Needs `secrets.py` on the board with WIFI_SSID, WIFI_PASSWORD and SENDER_HOST.
Start the sender first (tools/mock_sender.py during development), then turn the
wheel and watch the DRO move.

To run on power-up, copy the modules to the board and this file as main.py:

    python tools/sync_board.py     # copies micropython/pendant/pendant.py as main.py

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
    from quadrature import Quadrature
except ImportError:
    from pendant import link, protocol
    from pendant.buttons import ButtonPanel, PRESS, RELEASE, LONG_PRESS
    from pendant.jog import JogScheduler, TICK_MS
    from pendant.quadrature import Quadrature

import secrets

ENCODER_PIN_A = 2
ENCODER_PIN_B = 3

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
BUTTON_MAP = (
    (7, "feed_hold"),
    (8, "cycle_start"),
    # Zeroing fires on a long hold, never a tap. It rewrites the work offset,
    # and doing that by accident mid-job loses your datum. There is no manual
    # jog cancel button: the scheduler already cancels when the wheel stops.
    (9, "zero_axis"),
)

BUTTON_POLL_MS = 20

# Display. Optional throughout: if the panel is absent or miswired the pendant
# still jogs, which matters because the display is a convenience and the
# handwheel is the function.
DISPLAY_ENABLED = True
SPI_ID = 0
PIN_SCK, PIN_MOSI = 18, 19
PIN_CS, PIN_DC, PIN_RST, PIN_BL = 17, 20, 21, 22

# Capacitive touch, on its own I2C bus. Level converted on the module, so 3.3V
# logic is safe against it.
PIN_TOUCH_SDA, PIN_TOUCH_SCL = 10, 11
PIN_TOUCH_INT, PIN_TOUCH_RST = 12, 13
TOUCH_I2C_ID = 1

# Well inside a finger's dwell time, and cheap: the I2C read is a few
# hundred microseconds against the SPI the panel is already doing.
TOUCH_POLL_MS = 30

# How often the watchdog checks that the loop is still turning, and how late a
# wake-up has to be before it is worth reporting. 100 ms is five scheduler
# ticks - long enough that a busy tick does not trip it, short enough to catch
# a stall well before the link's 10 s deadline gives up.
WATCHDOG_MS = 100
STALL_WARN_MS = 100

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
DISPLAY_REFRESH_MS = 100

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
         "lag_enabled": True, "actual_feed": 0, "feed_collapses": 0,
         "last_collapse_dump": -60000, "planner_free": 0,
         "planner_min": 999, "planner_max": 0, "bf_warned": False,
         "stalls": 0, "worst_stall_ms": 0}


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
    if free is not None and scheduler is not None:
        state["planner_free"] = free
        scheduler.planner_free = free
        if free:
            if free < state["planner_min"]:
                state["planner_min"] = free
            if free > state["planner_max"]:
                state["planner_max"] = free

        # Said once, when enough frames have arrived to be sure. Judged on
        # never having seen a non-zero count rather than on this frame being
        # zero, because zero is also what a momentarily full planner reports -
        # and warning on that would be both wrong and alarming.
        #
        # It matters because without Bf: the pendant silently runs its degraded
        # path: emission pinned at the drain rate, the planner a block or two
        # deep, and motion that ripples for a reason nothing on screen explains.
        if (not state["bf_warned"] and state["status_frames"] > 20
                and scheduler.planner_capacity == 0):
            state["bf_warned"] = True
            link.log("controller is not reporting Bf: - planner depth is")
            link.log("  unknown, so motion will be rougher. Enable the")
            link.log("  buffer-state bit in $10 (add 2) and restart.")

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

    reference = state["_ref"]
    if reference is None or reference[0] != scheduler.axis:
        state["_ref"] = (scheduler.axis, wpos[index], scheduler.commanded_mm)
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

        spi = SPI(SPI_ID, baudrate=SPI_BAUD, polarity=0, phase=0,
                  sck=Pin(PIN_SCK), mosi=Pin(PIN_MOSI))
        # The panel is built here and handed to the layout, so fitting a
        # different one is a change to these two lines rather than to the
        # layout - which is the whole reason the display is injected.
        driver = ST7796S if PANEL == "st7796" else ILI9341
        display = driver(spi, PIN_CS, PIN_DC, PIN_RST, PIN_BL, ROTATION)
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
            point = touch.poll()
            if point is not None:
                hit = screen.zones.hit(point[0], point[1])
                if hit is not None:
                    kind, value = hit
                    if kind == "axis" and value != scheduler.axis:
                        # Changing axis mid-motion has to flush what is queued,
                        # or the old axis keeps running on after the operator
                        # has moved on. set_axis returns that cancel.
                        cancel = scheduler.set_axis(value)
                        if cancel and pendant_link:
                            pendant_link.send(cancel)
                        link.log("axis -> {}".format(value))
                    elif kind == "step":
                        scheduler.set_step(value)
                        link.log("step -> {} mm".format(value))
                    elif kind == "page":
                        # Reserved, not yet wired. Logged rather than ignored
                        # so the target is demonstrably live - a corner that
                        # silently does nothing is indistinguishable from one
                        # whose hit box is in the wrong place.
                        link.log("page tapped - nothing bound to it yet")
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

    while True:
        try:
            screen.set_link(pendant_link.connected if pendant_link else False)
            screen.set_state(state["machine_state"])
            screen.set_mode(scheduler.axis, scheduler.step,
                            scheduler.feed)
            if state["dro"]:
                screen.set_position(state["dro"])
        except Exception as exc:
            link.log("display error: {}: {}".format(type(exc).__name__, exc))
            return
        await asyncio.sleep_ms(DISPLAY_REFRESH_MS)


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

    Written only on change: the LED is on the CYW43, so each write is an SPI
    transaction competing with the radio. See the serial bridge for the
    latency this cost when it was rewritten every cycle.
    """
    led = Pin("LED", Pin.OUT)
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
        link.log(
            "{} | axis {} step {} F{:.0f}/act{} collapse={} | {} | detents={}"
            " dropped={} ({}% kept) lag={:.1f}/{:.1f}mm err={}"
            " sess={} stall={}/{}ms".format(
                "up" if pendant_link.connected else "DOWN",
                scheduler.axis, scheduler.step, scheduler.feed,
                state["actual_feed"], state["feed_collapses"], position,
                sent, dropped, kept,
                state["lag_mm"], state["peak_lag_mm"], encoder.errors,
                pendant_link.stats["sessions"], state["stalls"],
                state["worst_stall_ms"]))


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
