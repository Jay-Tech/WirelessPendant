"""Transparent serial <-> WiFi bridge for grblHAL / GRBL controllers.

Puts a Pico 2 W between a controller's UART and the network, so a USB-only
board becomes a networked one. Listens on TCP port 23 (grblHAL's telnet
convention) and shuttles raw bytes in both directions with no interpretation:

    GRBL controller  --UART-->  Pico 2 W  --TCP-->  sender application
                     <--UART--            <--TCP--

Deliberately byte-level rather than line-buffered. GRBL's real-time commands
('?' status, '!' hold, '~' resume, 0x18 reset, and the 0x8x feed/spindle
overrides) are single bytes that must land immediately and can arrive mid-line
- buffering to a newline would break status polling and, worse, delay a feed
hold. Nothing here waits for '\\n'.

Deploy:

    python -m mpremote connect id:7BE7DD09548134C0 fs cp micropython/secrets.py :secrets.py
    python -m mpremote connect id:7BE7DD09548134C0 run micropython/serial_bridge.py

To make it start on power-up, copy it as main.py instead:

    python -m mpremote connect id:7BE7DD09548134C0 fs cp micropython/serial_bridge.py :main.py

Wiring (Pico pin numbers are physical pins, not GPIO numbers):

    Pico GP0 / pin 1  TX  ->  controller RX
    Pico GP1 / pin 2  RX  <-  controller TX
    Pico GND / pin 3      ->  controller GND    (required - common ground)

The RP2350 is 3.3 V and NOT 5 V tolerant. Most grblHAL boards (STM32, Teensy,
ESP32) are 3.3 V and wire directly. A 5 V controller - notably an Arduino
Uno/Nano running classic GRBL - needs a level shifter on the Pico's RX line or
it will damage the pin.

No-hardware test: jumper GP0 to GP1 and everything you send comes back to you.
"""

import asyncio
import machine
import network
import time

# --- configuration -------------------------------------------------------

UART_ID = 0
UART_TX_PIN = 0
UART_RX_PIN = 1
UART_BAUDRATE = 115200

# Generous RX buffer. At 115200 baud the wire delivers ~11.5 bytes/ms, so 2 KB
# is roughly 175 ms of slack to cover any stall in the WiFi stack. Overflowing
# this means silently dropped bytes mid-job, which is worth over-provisioning
# against.
UART_RXBUF = 2048
UART_TXBUF = 1024

LISTEN_PORT = 23

# Poll interval when the UART is idle. 115200 baud cannot overflow a 2 KB
# buffer in 2 ms, and this keeps the core from spinning flat out.
IDLE_POLL_MS = 2

WIFI_JOIN_TIMEOUT_S = 20
WIFI_CHECK_INTERVAL_S = 5
STATS_INTERVAL_S = 30

# The status LED is driven over SPI on the CYW43, sharing the bus with the
# radio. Writing it is cheap but not free; set False to take it out of the
# picture entirely if you are chasing latency.
STATUS_LED = True

# --- state ---------------------------------------------------------------

uart = None
wlan = None
client = None  # active {"reader", "writer", "peer"} or None

stats = {"to_uart": 0, "to_net": 0, "dropped": 0, "sessions": 0}


def log(msg):
    print("[{:>8}] {}".format(time.ticks_ms() // 1000, msg))


# --- wifi ----------------------------------------------------------------


def wifi_connect():
    """Join the network from secrets.py. Returns the IP, or None on failure."""
    import secrets

    global wlan
    hostname = getattr(secrets, "HOSTNAME", None)
    if hostname:
        network.hostname(hostname)

    if wlan is None:
        wlan = network.WLAN(network.STA_IF)
    wlan.active(True)

    # Disable WiFi power management. The CYW43 defaults to a power-saving mode
    # that lets the radio doze between beacons, which is fine for a sensor
    # posting once a minute and awful here: it adds latency spikes of 80-100 ms
    # on top of an ~8 ms baseline. That shows up as a stuttering DRO and, on a
    # feed hold, as a delay you can feel. A mains-powered bridge has no reason
    # to save power.
    try:
        wlan.config(pm=network.WLAN.PM_NONE)
    except Exception as exc:
        log("note: could not disable wifi power save: {}".format(exc))

    if wlan.isconnected():
        return wlan.ifconfig()[0]

    log("joining {!r}...".format(secrets.WIFI_SSID))
    wlan.connect(secrets.WIFI_SSID, secrets.WIFI_PASSWORD)

    failures = {
        network.STAT_WRONG_PASSWORD: "wrong password",
        network.STAT_NO_AP_FOUND: "no such network in range",
        network.STAT_CONNECT_FAIL: "association failed",
    }

    deadline = time.ticks_add(time.ticks_ms(), WIFI_JOIN_TIMEOUT_S * 1000)
    while time.ticks_diff(deadline, time.ticks_ms()) > 0:
        status = wlan.status()
        if status == network.STAT_GOT_IP:
            ip = wlan.ifconfig()[0]
            log("online at {} ({} dBm)".format(ip, wlan.status("rssi")))
            if hostname:
                log("also reachable as {}.local".format(hostname))
            return ip
        if status in failures:
            log("wifi failed: {}".format(failures[status]))
            return None
        time.sleep_ms(250)

    log("wifi timed out after {}s".format(WIFI_JOIN_TIMEOUT_S))
    return None


async def wifi_watchdog():
    """Rejoin if the link drops. A bridge bolted to a machine has to self-heal."""
    while True:
        await asyncio.sleep(WIFI_CHECK_INTERVAL_S)
        if wlan is not None and not wlan.isconnected():
            log("wifi link lost - reconnecting")
            await drop_client("wifi lost")
            wifi_connect()


# --- client plumbing -----------------------------------------------------


async def drop_client(reason):
    """Close the active session, if any. Safe to call when there isn't one."""
    global client
    current, client = client, None
    if current is None:
        return
    log("closed {} ({})".format(current["peer"], reason))
    try:
        current["writer"].close()
        await current["writer"].wait_closed()
    except Exception:
        pass


def enable_nodelay(writer):
    """Disable Nagle so single-byte real-time commands go out immediately.

    Nagle would hold a lone '?' back waiting for more data to coalesce, adding
    tens of ms to every status poll and making the DRO visibly stutter. The
    socket lives on a private attribute of MicroPython's Stream, and the
    constant is not exposed on every port, so this is best-effort.
    """
    try:
        import socket

        sock = getattr(writer, "s", None)
        if sock is None:
            return False
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        return True
    except Exception:
        return False


async def handle_client(reader, writer):
    """One TCP session: pump network -> UART until it closes."""
    global client

    peer = writer.get_extra_info("peername")
    peer = "{}:{}".format(peer[0], peer[1]) if peer else "?"

    # GRBL is a single-session protocol - two senders would interleave commands
    # and corrupt the parser state. New connection wins, because the common case
    # is a stale half-open socket from a crashed client, and refusing would lock
    # the machine out until that socket finally times out.
    if client is not None:
        await drop_client("superseded by {}".format(peer))

    if not enable_nodelay(writer):
        log("note: could not set TCP_NODELAY - expect extra status latency")

    # Discard anything the controller said while nobody was listening, so the
    # session starts on a clean boundary rather than mid-message.
    if uart.any():
        uart.read()

    client = {"reader": reader, "writer": writer, "peer": peer}
    stats["sessions"] += 1
    log("client {} connected".format(peer))

    try:
        while True:
            data = await reader.read(256)
            if not data:
                break  # clean EOF
            if client is None or client["peer"] != peer:
                break  # superseded while we were awaiting
            uart.write(data)
            stats["to_uart"] += len(data)
    except Exception as exc:
        log("client {} error: {}: {}".format(peer, type(exc).__name__, exc))
    finally:
        if client is not None and client["peer"] == peer:
            await drop_client("disconnected")


async def uart_to_net():
    """Pump UART -> network. One task for the lifetime of the bridge."""
    while True:
        pending = uart.any()
        if not pending:
            await asyncio.sleep_ms(IDLE_POLL_MS)
            continue

        data = uart.read(pending)
        if not data:
            await asyncio.sleep_ms(IDLE_POLL_MS)
            continue

        current = client
        if current is None:
            # Nothing listening. Drain rather than let the buffer wrap, but
            # count it - a large number here means the controller is chattering
            # to nobody, which usually means the sender is not connected.
            stats["dropped"] += len(data)
            continue

        try:
            current["writer"].write(data)
            await current["writer"].drain()
            stats["to_net"] += len(data)
        except Exception as exc:
            log("write to {} failed: {}".format(current["peer"], exc))
            await drop_client("write failed")

        await asyncio.sleep_ms(0)  # yield without idling


async def status_led():
    """fast blink = no wifi, slow heartbeat = idle, solid = client attached.

    Every write here is an SPI transaction to the CYW43 - the LED is on the
    WiFi chip, not an RP2350 GPIO - so it contends with the radio for the same
    bus and driver lock. Writing only on an actual state change keeps that
    traffic to a few transactions per session instead of one every 200 ms for
    the whole time a client is attached.
    """
    led = machine.Pin("LED", machine.Pin.OUT)
    lit = None

    def show(on):
        nonlocal lit
        if lit is not on:
            led.on() if on else led.off()
            lit = on

    while True:
        if not STATUS_LED:
            return
        if wlan is None or not wlan.isconnected():
            show(not lit)
            await asyncio.sleep_ms(100)
        elif client is None:
            show(True)
            await asyncio.sleep_ms(50)
            show(False)
            await asyncio.sleep_ms(1950)
        else:
            show(True)  # solid, and written exactly once
            await asyncio.sleep_ms(200)


async def report_stats():
    while True:
        await asyncio.sleep(STATS_INTERVAL_S)
        log(
            "sessions={} net->uart={}B uart->net={}B dropped={}B{}".format(
                stats["sessions"],
                stats["to_uart"],
                stats["to_net"],
                stats["dropped"],
                "" if client is None else "  [{}]".format(client["peer"]),
            )
        )


async def main():
    global uart

    print("\ngrblHAL serial <-> WiFi bridge")
    print("=" * 40)

    uart = machine.UART(
        UART_ID,
        baudrate=UART_BAUDRATE,
        tx=machine.Pin(UART_TX_PIN),
        rx=machine.Pin(UART_RX_PIN),
        rxbuf=UART_RXBUF,
        txbuf=UART_TXBUF,
    )
    log(
        "uart{} on GP{}/GP{} at {} baud".format(
            UART_ID, UART_TX_PIN, UART_RX_PIN, UART_BAUDRATE
        )
    )

    ip = wifi_connect()
    if ip is None:
        log("no network - check secrets.py. giving up.")
        return

    await asyncio.start_server(handle_client, "0.0.0.0", LISTEN_PORT)
    log("listening on {}:{}".format(ip, LISTEN_PORT))
    log("point the sender's TCP adapter at that address, then Ctrl-C to stop")

    await asyncio.gather(
        uart_to_net(),
        wifi_watchdog(),
        status_led(),
        report_stats(),
    )


try:
    asyncio.run(main())
except KeyboardInterrupt:
    print("\nstopped.")
finally:
    asyncio.new_event_loop()
