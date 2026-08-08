"""Find the right MADCTL for this panel by trying them all.

Orientation and colour order both live in one register, and both were wrong on
first bring-up: the layout came up landscape when portrait was asked for, and
the colours did not match the panel this replaced. Guessing at one then the
other costs a wiring session each time, so this draws an unambiguous pattern
under every candidate and lets the panel answer.

    python tools/on_board.py micropython/pendant/probe_madctl.py

Each step prints its value and holds the pattern for a few seconds:

    a RED bar along the TOP edge
    a GREEN square in the TOP-LEFT corner
    a BLUE square in the BOTTOM-RIGHT corner
    a white strip down the LEFT edge

Note the value where all four land where they say. If the bar is blue rather
than red, that candidate has the colour order wrong - the BGR variants and the
RGB ones are both included, so keep going.

Then set ROTATIONS in st7796.py to that value.
"""

import time

from machine import SPI, Pin

try:
    from st7796 import ST7796S
    from ili9341 import BLACK, WHITE, RED, GREEN, BLUE
except ImportError:
    from pendant.st7796 import ST7796S
    from pendant.ili9341 import BLACK, WHITE, RED, GREEN, BLUE

SPI_ID = 0
SPI_BAUD = 20_000_000
PIN_SCK, PIN_MOSI = 18, 19
PIN_CS, PIN_DC, PIN_RST, PIN_BL = 17, 20, 21, 22

_MADCTL = 0x36

# MY 0x80, MX 0x40, MV 0x20, BGR 0x08.
#
# MV is the one that decides orientation: set, rows and columns are exchanged
# and the panel is landscape. BGR decides whether red and blue are swapped, and
# whether a module needs it depends on how the glass is wired rather than on
# the controller - which is why both are here rather than assumed.
CANDIDATES = (
    (0x48, "MX  BGR   portrait"),
    (0x40, "MX  RGB   portrait"),
    (0x88, "MY  BGR   portrait flipped"),
    (0x80, "MY  RGB   portrait flipped"),
    (0x28, "MV  BGR   landscape"),
    (0x20, "MV  RGB   landscape"),
    (0xE8, "MY MX MV BGR  landscape flipped"),
    (0xE0, "MY MX MV RGB  landscape flipped"),
)

HOLD_SECONDS = 5


def pattern(display):
    """Draw something whose orientation and colours are unmistakable."""
    display.fill(BLACK)
    width, height = display.width, display.height
    edge = 40

    # Red bar across the top: names the top edge and proves red is red.
    display.fill_rect(0, 0, width, 18, RED)
    # Green at top-left, blue at bottom-right: names the diagonal, so a flip
    # shows up even when the bar looks right.
    display.fill_rect(0, 22, edge, edge, GREEN)
    display.fill_rect(width - edge, height - edge, edge, edge, BLUE)
    # A strip down the left edge: distinguishes a mirror from a rotation, which
    # the corner squares alone cannot.
    display.fill_rect(0, height // 2 - 40, 10, 80, WHITE)


def main():
    print("\nMADCTL probe")
    print("=" * 46)
    print("Looking for: RED bar on TOP, GREEN square TOP-LEFT,")
    print("             BLUE square BOTTOM-RIGHT, white strip LEFT edge.\n")

    spi = SPI(SPI_ID, baudrate=SPI_BAUD, polarity=0, phase=0,
              sck=Pin(PIN_SCK), mosi=Pin(PIN_MOSI))
    display = ST7796S(spi, PIN_CS, PIN_DC, PIN_RST, PIN_BL, 0)

    for value, description in CANDIDATES:
        # Width and height follow MV, or the window commands would address a
        # frame the wrong way round and everything would wrap.
        if value & 0x20:
            display.width, display.height = 480, 320
        else:
            display.width, display.height = 320, 480

        display._write(_MADCTL, bytes([value]))
        pattern(display)

        print("  0x{:02X}   {:<30} {}x{}".format(
            value, description, display.width, display.height))
        time.sleep(HOLD_SECONDS)

    display.fill(BLACK)
    print("\nNote the value that looked right and tell me which it was.")
    print("If none did, say what the closest one got wrong - a mirrored")
    print("image and a rotated one need different bits.")


main()
