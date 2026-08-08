"""The pendant itself: handwheel -> jog messages -> sender.

Wires together the pieces that were built and tested separately - the
quadrature decoder, the jog scheduler, and the network link - and runs them as
concurrent tasks.

    python -m mpremote connect id:7BE7DD09548134C0 run micropython/pendant/pendant.py

Needs `secrets.py` on the board with WIFI_SSID, WIFI_PASSWORD and SENDER_HOST.
Start the sender first (tools/mock_sender.py during development), then turn the
wheel and watch the DRO move.

To run on power-up, copy the modules to the board and this file as main.py:

    python -m mpremote connect id:7BE7DD09548134C0 fs cp micropython/pendant/pendant.py :main.py

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
BUTTON_MAP = (
    (4, "axis_next"),
    (5, "step_down"),
    (6, "step_up"),
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
SPI_BAUD = 20_000_000
DISPLAY_REFRESH_MS = 100

STATUS_REPORT_S = 15

encoder = None
scheduler = None
pendant_link = None
screen = None

state = {"dro": None, "machine_state": "?", "status_frames": 0,
         "lag_mm": 0.0, "peak_lag_mm": 0.0, "_ref": None,
         "lag_enabled": True}


def on_message(message):
    if message.get("t") != protocol.T_STATUS:
        return
    state["status_frames"] += 1
    wpos = message.get("wpos")
    state["dro"] = wpos
    state["machine_state"] = message.get("state", "?")

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
    if lag > state["peak_lag_mm"]:
        state["peak_lag_mm"] = lag


def handle_button(action, event):
    """Map one button event onto pendant state or a message to the sender."""
    # Axis and step live entirely on the pendant: they change what future jog
    # messages say, and the sender has no opinion about them.
    if event == PRESS and action == "axis_next":
        index = AXIS_ORDER.index(scheduler.axis) if scheduler.axis in AXIS_ORDER else -1
        cancel = scheduler.set_axis(AXIS_ORDER[(index + 1) % len(AXIS_ORDER)])
        link.log("axis -> {}".format(scheduler.axis))
        return cancel

    # Holding the axis button toggles jog behaviour. On a button rather than a
    # constant because the two only differ by feel, and the comparison has to
    # happen standing at the machine - `mpremote run` holds the serial port for
    # the whole session, so there is no REPL available to flip it live.
    if event == LONG_PRESS and action == "axis_next":
        scheduler.cancel_on_stop = not scheduler.cancel_on_stop
        link.log("jog mode -> {}".format(
            "HALT on stop" if scheduler.cancel_on_stop else "QUEUE and execute"))
        return None

    if event == PRESS and action == "step_up":
        link.log("step -> {} mm".format(scheduler.step_up()))
        return None

    if event == PRESS and action == "step_down":
        link.log("step -> {} mm".format(scheduler.step_down()))
        return None

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


def start_display():
    """Bring up the panel, or return None and carry on without it."""
    if not DISPLAY_ENABLED:
        return None
    try:
        from machine import SPI
        try:
            from screen import DroScreen
        except ImportError:
            from pendant.screen import DroScreen

        spi = SPI(SPI_ID, baudrate=SPI_BAUD, polarity=0, phase=0,
                  sck=Pin(PIN_SCK), mosi=Pin(PIN_MOSI))
        panel = DroScreen(spi, PIN_CS, PIN_DC, PIN_RST, PIN_BL)
        panel.splash("connecting...")
        link.log("display on SPI{} at {} MHz".format(
            SPI_ID, SPI_BAUD // 1000000))
        return panel
    except Exception as exc:
        # A missing or miswired panel must not stop the pendant jogging.
        link.log("display unavailable ({}: {}) - running headless".format(
            type(exc).__name__, exc))
        return None


async def refresh_display():
    """Push current state to the panel.

    Polls rather than being driven by events because the screen redraws only
    changed characters anyway - a poll that finds nothing different costs a few
    string comparisons, and this keeps display work off the message handler.
    """
    if screen is None:
        return

    # Replace the splash with the live layout now the panel is known good.
    screen.__init__(screen.display._spi, PIN_CS, PIN_DC, PIN_RST, PIN_BL)

    while True:
        try:
            screen.set_link(pendant_link.connected if pendant_link else False)
            screen.set_state(state["machine_state"])
            screen.set_mode(scheduler.axis, scheduler.step,
                            scheduler.cancel_on_stop, scheduler.feed)
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
                       scheduler.cancel_on_stop,
                       pendant_link.stats["sessions"])
            if current != last:
                last = current
                pendant_link.send(protocol.mode(
                    scheduler.axis, scheduler.step, scheduler.cancel_on_stop))
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
        link.log(
            "{} | axis {} step {} F{:.0f} | {} | detents={} dropped={} ({}% kept)"
            " lag={:.1f}/{:.1f}mm err={}".format(
                "up" if pendant_link.connected else "DOWN",
                scheduler.axis, scheduler.step, scheduler.feed, position,
                sent, dropped, kept,
                state["lag_mm"], state["peak_lag_mm"], encoder.errors))


async def main():
    global encoder, scheduler, pendant_link, screen

    print("\ngrblHAL wireless pendant")
    print("=" * 46)

    screen = start_display()
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
