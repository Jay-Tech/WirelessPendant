"""Bring-up test for the ILI9341 - no network, no encoder.

Walks through colour fills, the DRO layout, and a timed refresh, so a wiring
fault is distinguishable from a driver fault. What each stage tells you:

* Colour bars wrong or absent -> wiring, CS/DC, or SPI speed.
* Red and blue swapped -> the panel is RGB rather than BGR; drop 0x08 from the
  MADCTL values in ili9341.py.
* Picture but no light -> backlight pin.
* Everything fine but slow -> raise SPI_BAUD.

    python -m mpremote connect id:7BE7DD09548134C0 run micropython/pendant/selftest_display.py
"""

import time

from machine import Pin, SPI

try:
    from ili9341 import BLACK, WHITE, RED, GREEN, BLUE, AMBER, GREY
    from screen import DroScreen
except ImportError:
    from pendant.ili9341 import BLACK, WHITE, RED, GREEN, BLUE, AMBER, GREY
    from pendant.screen import DroScreen

SPI_ID = 0
PIN_SCK = 18
PIN_MOSI = 19
PIN_CS = 17
PIN_DC = 20
PIN_RST = 21
PIN_BL = 22

# Conservative for breadboard wiring. The panel handles far more on a short,
# tidy harness; raise it once the layout is permanent.
SPI_BAUD = 20_000_000


def main():
    print("\nILI9341 display self-test")
    print("=" * 46)
    print("  SPI{}  sck=GP{} mosi=GP{}".format(SPI_ID, PIN_SCK, PIN_MOSI))
    print("  cs=GP{} dc=GP{} rst=GP{} bl=GP{}".format(
        PIN_CS, PIN_DC, PIN_RST, PIN_BL))
    print("  {} MHz\n".format(SPI_BAUD // 1000000))

    spi = SPI(SPI_ID, baudrate=SPI_BAUD, polarity=0, phase=0,
              sck=Pin(PIN_SCK), mosi=Pin(PIN_MOSI))

    started = time.ticks_ms()
    screen = DroScreen(spi, PIN_CS, PIN_DC, PIN_RST, PIN_BL)
    print("  panel initialised in {} ms".format(
        time.ticks_diff(time.ticks_ms(), started)))
    print("  {}x{}".format(screen.display.width, screen.display.height))

    # Full-screen fills: the coarsest check that the bus works at all.
    print("\n  colour fills - watch the panel")
    for name, color in (("red", RED), ("green", GREEN), ("blue", BLUE),
                        ("white", WHITE), ("black", BLACK)):
        began = time.ticks_ms()
        screen.display.fill(color)
        print("    {:<6} {} ms".format(
            name, time.ticks_diff(time.ticks_ms(), began)))
        time.sleep_ms(250)

    print("\n  red then blue should have appeared in that order;")
    print("  if they were swapped the panel is RGB, not BGR")

    # Rebuild the layout after the fills wiped it.
    screen = DroScreen(spi, PIN_CS, PIN_DC, PIN_RST, PIN_BL)
    screen.set_link(True)
    screen.set_mode("X", 0.1)
    screen.set_state("Idle")

    print("\n  DRO layout - positions should count up")
    began = time.ticks_ms()
    frames = 0
    value = 0.0
    while time.ticks_diff(time.ticks_ms(), began) < 3000:
        value += 0.137
        screen.set_position((value, -value / 2, value / 4))
        frames += 1
    elapsed = time.ticks_diff(time.ticks_ms(), began)
    print("    {} updates in {} ms = {:.1f} fps".format(
        frames, elapsed, frames * 1000.0 / elapsed))
    print("    (a DRO needs 10; anything above that is headroom)")

    print("\n  state colours")
    for state in ("Idle", "Jog", "Hold", "Alarm", "Run"):
        screen.set_state(state)
        time.sleep_ms(400)

    print("\n  axis highlight")
    for axis in ("X", "Y", "Z", "X"):
        screen.set_mode(axis, 0.1)
        time.sleep_ms(400)

    screen.set_state("Idle")
    screen.set_link(False)
    print("\n  done - panel should show a DRO with LINK DOWN in red")
    return 0


raise SystemExit(main())
