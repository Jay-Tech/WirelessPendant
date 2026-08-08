"""Bring-up test for the ST7796S panel - no network, no encoder, no touch.

Runs through the failure modes in the order they are worth ruling out, because
a blank panel gives no clue which one it is:

  * backlight only - the panel lights but nothing is drawn, which points at
    MOSI, SCK or DC rather than power
  * solid colours - proves addressing and the pixel format
  * corners - proves the window commands and the reported geometry, and shows
    immediately if width and height are transposed
  * colour order - red drawn first, so a blue square means the BGR bit is wrong
  * the DRO layout itself

    python tools/on_board.py micropython/pendant/selftest_st7796.py

Wiring is unchanged from the ILI9341 except the panel's RS pin is the DC line:

    LCD_CS -> GP17    SCK -> GP18    SDI/MOSI -> GP19
    LCD_RS -> GP20    LCD_RST -> GP21    LED -> GP22
    VCC    -> VBUS (5V), not 3V3
"""

import time

from machine import SPI, Pin

try:
    from st7796 import ST7796S
    from ili9341 import BLACK, WHITE, RED, GREEN, BLUE, AMBER
    from screen import DroScreen
except ImportError:
    from pendant.st7796 import ST7796S
    from pendant.ili9341 import BLACK, WHITE, RED, GREEN, BLUE, AMBER
    from pendant.screen import DroScreen

SPI_ID = 0
SPI_BAUD = 20_000_000
PIN_SCK, PIN_MOSI = 18, 19
PIN_CS, PIN_DC, PIN_RST, PIN_BL = 17, 20, 21, 22

# Native portrait. The touch controller reports in this orientation, so
# matching it here keeps the coordinate mapping a straight pass-through.
ROTATION = 0


def pause(seconds, message):
    print("  {}".format(message))
    time.sleep(seconds)


def main():
    print("\nST7796S display self-test")
    print("=" * 46)
    print("SPI{} at {} MHz, rotation {}".format(
        SPI_ID, SPI_BAUD // 1_000_000, ROTATION))

    spi = SPI(SPI_ID, baudrate=SPI_BAUD, polarity=0, phase=0,
              sck=Pin(PIN_SCK), mosi=Pin(PIN_MOSI))
    display = ST7796S(spi, PIN_CS, PIN_DC, PIN_RST, PIN_BL, ROTATION)

    print("panel reports {} x {}".format(display.width, display.height))
    if (display.width, display.height) != (320, 480):
        print("  ! expected 320 x 480 in portrait - check ROTATION")

    print("\n1. backlight")
    display.backlight(False)
    pause(1, "off")
    display.backlight(True)
    pause(1, "on - if nothing lit, the LED pin or VCC is the problem")

    print("\n2. solid fills")
    for color, name in ((RED, "red"), (GREEN, "green"), (BLUE, "blue"),
                        (WHITE, "white"), (BLACK, "black")):
        display.fill(color)
        pause(0.6, name)

    print("\n3. corners")
    print("  red top-left, green top-right, blue bottom-left, white "
          "bottom-right")
    box = 60
    display.fill(BLACK)
    display.fill_rect(0, 0, box, box, RED)
    display.fill_rect(display.width - box, 0, box, box, GREEN)
    display.fill_rect(0, display.height - box, box, box, BLUE)
    display.fill_rect(display.width - box, display.height - box, box, box,
                      WHITE)
    pause(4, "all four should touch the edges, none wrapped or clipped")

    print("\n4. colour order")
    print("  a red bar is drawn - if it looks blue, the BGR bit is wrong")
    display.fill(BLACK)
    display.fill_rect(20, display.height // 2 - 30, display.width - 40, 60, RED)
    pause(3, "red bar")

    print("\n5. the DRO layout")
    screen = DroScreen(display)
    screen.set_state("Idle")
    screen.set_link(True)
    for step in range(40):
        screen.set_position((step * 1.37, -step * 0.5, 12.0 + step * 0.01))
        screen.set_mode("X", 0.5, True, 2000 + step * 175)
        time.sleep_ms(80)
    pause(2, "three axis rows at the top, status anchored at the bottom")

    print("\ndone. The gap in the middle is where the touch zones go.")


main()
