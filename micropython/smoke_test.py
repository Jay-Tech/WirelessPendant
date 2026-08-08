"""Pico 2 W smoke test.

Walks the board through six stages and prints a pass/fail summary:

    1. identity   - firmware, chip, clock, RAM, filesystem
    2. led        - blink the onboard LED (visible proof of life)
    3. temp       - read the RP2350 internal temperature sensor
    4. wifi_scan  - prove the radio powers up and hears something
    5. wifi_join  - associate and get a DHCP lease
    6. net_io     - DNS + TCP + HTTP, end to end

Stages 4-6 need a `secrets.py` next to this file (see secrets.example.py).
Without it they report SKIP and the rest still runs, so the script is
useful before you've decided on credentials.

Run it without installing anything on the board:

    python tools/on_board.py smoke_test.py

Target the board by ID, not `connect auto` - auto grabs the first USB serial
device, which can be an attached CNC controller rather than the Pico. Run
`python -m mpremote devs` to list boards; Pico reports a 2e8a: vendor ID.

Note that `run` executes this file from your PC but imports resolve on the
board, so `secrets.py` does need to be copied over first.
"""

import binascii
import gc
import machine
import os
import sys
import time

# ADC channel 4 is the RP2350's on-die temperature sensor. Channels 0-2 are
# GP26-28; channel 3 is unavailable here because GP29 belongs to the WiFi SPI.
_TEMP_ADC_CHANNEL = 4

# Conversion from the RP2350 datasheet. Uncalibrated part-to-part, so treat
# the result as +/- a few degrees - it proves the ADC works, not much more.
_TEMP_V_AT_27C = 0.706
_TEMP_V_PER_DEGREE = 0.001721

_ADC_VREF = 3.3
_ADC_FULL_SCALE = 65535

_WIFI_JOIN_TIMEOUT_S = 20
_HTTP_HOST = "example.com"
_HTTP_TIMEOUT_S = 10

results = []


def stage(name, fn):
    """Run one stage, record its outcome, and keep going regardless."""
    print("\n=== {} ===".format(name))
    try:
        outcome = fn()
    except Exception as exc:  # noqa: BLE001 - a smoke test wants every failure
        print("  FAIL: {}: {}".format(type(exc).__name__, exc))
        results.append((name, "FAIL"))
        return None
    status, value = outcome if isinstance(outcome, tuple) else ("PASS", outcome)
    results.append((name, status))
    return value


def identity():
    uname = os.uname()
    print("  firmware:   {}".format(uname.version))
    print("  port:       {} / {}".format(uname.sysname, uname.machine))
    version = ".".join(str(n) for n in sys.implementation.version[:3])
    print("  python:     {}".format(version))
    print("  clock:      {:.1f} MHz".format(machine.freq() / 1000000))
    print("  unique id:  {}".format(binascii.hexlify(machine.unique_id()).decode()))

    gc.collect()
    free = gc.mem_free()
    used = gc.mem_alloc()
    print("  heap:       {} B free of {} B".format(free, free + used))

    stat = os.statvfs("/")
    block_size, total_blocks, free_blocks = stat[0], stat[2], stat[3]
    print(
        "  filesystem: {} KB free of {} KB".format(
            block_size * free_blocks // 1024, block_size * total_blocks // 1024
        )
    )
    return True


def led():
    # On the Pico 2 W the LED hangs off the CYW43 radio chip, not an RP2350
    # GPIO - so it is Pin("LED"), never Pin(25). That difference is the single
    # most common reason a copied Pico tutorial does nothing on a W board.
    pin = machine.Pin("LED", machine.Pin.OUT)
    print("  blinking 5x on Pin('LED')...")
    for _ in range(5):
        pin.on()
        time.sleep_ms(120)
        pin.off()
        time.sleep_ms(120)
    print("  done - did you see it?")
    return True


def temp():
    adc = machine.ADC(_TEMP_ADC_CHANNEL)
    raw = adc.read_u16()
    volts = raw * _ADC_VREF / _ADC_FULL_SCALE
    celsius = 27 - (volts - _TEMP_V_AT_27C) / _TEMP_V_PER_DEGREE
    print("  raw: {}  ->  {:.2f} V  ->  {:.1f} C".format(raw, volts, celsius))

    # A dead or misread sensor pins to a rail; anything in this window means
    # the ADC path is genuinely alive.
    if not -20 < celsius < 120:
        return "FAIL", celsius
    return celsius


def _load_secrets():
    try:
        import secrets
    except ImportError:
        return None
    return secrets


def _wlan():
    import network

    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)
    return wlan


def wifi_scan():
    if _load_secrets() is None:
        print("  SKIP: no secrets.py on the board")
        return "SKIP", None

    wlan = _wlan()
    networks = wlan.scan()
    print("  found {} network(s):".format(len(networks)))
    # scan() yields (ssid, bssid, channel, rssi, security, hidden)
    for ssid, _bssid, channel, rssi, _sec, _hidden in sorted(
        networks, key=lambda n: n[3], reverse=True
    )[:8]:
        name = ssid.decode() or "<hidden>"
        print("    {:>4} dBm  ch{:<3} {}".format(rssi, channel, name))
    if not networks:
        return "FAIL", 0
    return len(networks)


def wifi_join():
    import network

    secrets = _load_secrets()
    if secrets is None:
        print("  SKIP: no secrets.py on the board")
        return "SKIP", None

    hostname = getattr(secrets, "HOSTNAME", None)
    if hostname:
        network.hostname(hostname)

    wlan = _wlan()
    print("  joining {!r}...".format(secrets.WIFI_SSID))
    wlan.connect(secrets.WIFI_SSID, secrets.WIFI_PASSWORD)

    failures = {
        network.STAT_WRONG_PASSWORD: "wrong password",
        network.STAT_NO_AP_FOUND: "no such network in range",
        network.STAT_CONNECT_FAIL: "association failed",
    }

    deadline = time.ticks_add(time.ticks_ms(), _WIFI_JOIN_TIMEOUT_S * 1000)
    while time.ticks_diff(deadline, time.ticks_ms()) > 0:
        status = wlan.status()
        if status == network.STAT_GOT_IP:
            break
        if status in failures:
            print("  FAIL: {}".format(failures[status]))
            return "FAIL", None
        time.sleep_ms(250)
    else:
        print("  FAIL: timed out after {}s".format(_WIFI_JOIN_TIMEOUT_S))
        return "FAIL", None

    ip, netmask, gateway, dns = wlan.ifconfig()
    mac = binascii.hexlify(wlan.config("mac"), ":").decode()
    print("  ip:      {}".format(ip))
    print("  netmask: {}".format(netmask))
    print("  gateway: {}".format(gateway))
    print("  dns:     {}".format(dns))
    print("  mac:     {}".format(mac))
    print("  rssi:    {} dBm".format(wlan.status("rssi")))
    if hostname:
        print("  host:    {} (try http://{}.local)".format(hostname, hostname))
    return ip


def net_io():
    import socket

    if _load_secrets() is None:
        print("  SKIP: no secrets.py on the board")
        return "SKIP", None

    # Plain HTTP on purpose. TLS works on this chip but pulls in a much larger
    # code path, and the point here is to prove DNS + TCP + sockets, not crypto.
    addr = socket.getaddrinfo(_HTTP_HOST, 80)[0][-1]
    print("  {} resolves to {}".format(_HTTP_HOST, addr[0]))

    sock = socket.socket()
    sock.settimeout(_HTTP_TIMEOUT_S)
    try:
        started = time.ticks_ms()
        sock.connect(addr)
        sock.send(
            "GET / HTTP/1.0\r\nHost: {}\r\nConnection: close\r\n\r\n".format(
                _HTTP_HOST
            ).encode()
        )
        response = sock.recv(128)
        elapsed = time.ticks_diff(time.ticks_ms(), started)
    finally:
        sock.close()

    status_line = response.split(b"\r\n", 1)[0].decode()
    print("  {}  ({} ms round trip)".format(status_line, elapsed))
    if b"200" not in response[:64]:
        return "FAIL", status_line
    return status_line


def main():
    print("\nPico 2 W smoke test")
    print("=" * 40)

    stage("identity", identity)
    stage("led", led)
    stage("temp", temp)
    stage("wifi_scan", wifi_scan)
    stage("wifi_join", wifi_join)
    stage("net_io", net_io)

    print("\n" + "=" * 40)
    print("summary")
    for name, status in results:
        print("  {:<10} {}".format(name, status))

    failed = [n for n, s in results if s == "FAIL"]
    skipped = [n for n, s in results if s == "SKIP"]
    print()
    if failed:
        print("{} stage(s) FAILED: {}".format(len(failed), ", ".join(failed)))
    elif skipped:
        print("all run stages passed; skipped: {}".format(", ".join(skipped)))
    else:
        print("all stages passed - board is healthy.")


main()
