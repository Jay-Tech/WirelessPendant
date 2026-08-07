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

No display or buttons are required. Axis and step size are set below and can be
changed live from the REPL via the module-level `scheduler` object.
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
    (9, "jog_cancel"),
)

BUTTON_POLL_MS = 20

STATUS_REPORT_S = 15

encoder = None
scheduler = None
pendant_link = None

state = {"dro": None, "machine_state": "?", "status_frames": 0}


def on_message(message):
    if message.get("t") != protocol.T_STATUS:
        return
    state["status_frames"] += 1
    state["dro"] = message.get("wpos")
    state["machine_state"] = message.get("state", "?")


def handle_button(action, event):
    """Map one button event onto pendant state or a message to the sender."""
    # Axis and step live entirely on the pendant: they change what future jog
    # messages say, and the sender has no opinion about them.
    if event == PRESS and action == "axis_next":
        index = AXIS_ORDER.index(scheduler.axis) if scheduler.axis in AXIS_ORDER else -1
        cancel = scheduler.set_axis(AXIS_ORDER[(index + 1) % len(AXIS_ORDER)])
        link.log("axis -> {}".format(scheduler.axis))
        return cancel

    if event == PRESS and action == "step_up":
        link.log("step -> {} mm".format(scheduler.step_up()))
        return None

    if event == PRESS and action == "step_down":
        link.log("step -> {} mm".format(scheduler.step_down()))
        return None

    if event == PRESS and action == "jog_cancel":
        link.log("jog cancel")
        return protocol.jog_cancel()

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
        link.log("{} | axis {} step {} | {} | jogs={} detents={} err={}".format(
            "up" if pendant_link.connected else "DOWN",
            scheduler.axis, scheduler.step, position,
            scheduler.stats["messages"], scheduler.stats["detents"],
            encoder.errors))


async def main():
    global encoder, scheduler, pendant_link

    print("\ngrblHAL wireless pendant")
    print("=" * 46)

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
