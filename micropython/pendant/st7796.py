"""ST7796S driver for the MSP3525/MSP3526 3.5" 320x480 IPS panel.

Subclasses the ILI9341 driver rather than repeating it. That is not a
convenience: addressing, pixel format and orientation are MIPI DCS commands and
carry the same numbers and meanings on both parts, so the transport, windowing,
fill and blit paths are genuinely the same code rather than two copies that
drift. What differs is the power-on sequence and the geometry, and those are
all this overrides.

The panel wants 5V on VCC. It regulates to 3.3V on board, and the module manual
is explicit that feeding it 3.3V leaves that rail short and dims the backlight.
Logic stays at 3.3V.

    display = ST7796S(spi, cs=17, dc=20, rst=21, backlight=22, rotation=0)

Rotation 0 is native portrait, 320 wide by 480 tall, which is also how the
FT6336U touch controller reports coordinates on this module - measured, not
assumed: a corner-to-corner sweep gave x 0..319 and y 7..478. Running the
display in the same orientation means touch maps straight through with no swap
or flip, so that is the default here.
"""

import time

try:
    from ili9341 import ILI9341
except ImportError:
    from pendant.ili9341 import ILI9341

_SWRESET = const(0x01)
_SLPOUT = const(0x11)
_INVOFF = const(0x20)
_INVON = const(0x21)
_DISPON = const(0x29)
_MADCTL = const(0x36)
_PIXFMT = const(0x3A)

# ST7796S-specific. The extended registers below 0xC0 are locked at reset and
# silently ignore writes until CSCON opens them, which is the usual reason a
# panel that accepts every command still shows nothing.
_CSCON = const(0xF0)      # command set control
_INVCTR = const(0xB4)     # display inversion control
_DFUNCTR = const(0xB6)    # display function control
_DOCA = const(0xE8)       # display output ctrl adjust
_PWCTR2 = const(0xC1)
_PWCTR3 = const(0xC2)
_VCMPCTL = const(0xC5)    # VCOM control
_GAMMAP = const(0xE0)     # positive gamma
_GAMMAN = const(0xE1)     # negative gamma

_UNLOCK_1 = b"\xc3"
_UNLOCK_2 = b"\x96"
_LOCK_1 = b"\x3c"
_LOCK_2 = b"\x69"


class ST7796S(ILI9341):
    """320x480 IPS panel. Same command set, different init and geometry."""

    # MADCTL values are inherited in meaning from the base table - those bits
    # are standard - and only the dimensions change, this panel being 320x480
    # native rather than 240x320.
    ROTATIONS = {
        0: (0x48, 320, 480),
        90: (0x28, 480, 320),
        180: (0x88, 320, 480),
        270: (0xE8, 480, 320),
    }

    def __init__(self, spi, cs, dc, rst=None, backlight=None, rotation=0):
        super().__init__(spi, cs, dc, rst, backlight, rotation)

    def _init_panel(self, madctl):
        self._write(_SWRESET)
        time.sleep_ms(120)
        self._write(_SLPOUT)
        time.sleep_ms(120)

        # Open the extended register set. Without this the power, gamma and
        # output-adjust writes below are accepted and discarded, and the panel
        # comes up either blank or badly washed out depending on the batch.
        self._write(_CSCON, _UNLOCK_1)
        self._write(_CSCON, _UNLOCK_2)

        self._write(_MADCTL, bytes([madctl]))
        self._write(_PIXFMT, b"\x55")            # 16 bits per pixel, RGB565

        # Inversion ON. Not the same thing as _INVCTR below, and the whole of
        # the first bring-up failure: the IPS glass on this module drives the
        # complement of what is written, so without this every colour comes out
        # as its photographic negative - red rendered cyan, green magenta, blue
        # brown, and the white edge strip black - while the geometry is
        # perfectly correct.
        #
        # That combination reads like a colour-order fault and is not one. The
        # BGR bit swaps red and blue; it cannot turn green into magenta, and it
        # certainly cannot turn white into black. Any two of those four told us
        # which register it was.
        #
        # TN glass on the same controller does not need it, which is why it is
        # missing from most reference inits.
        self._write(_INVON)

        self._write(_INVCTR, b"\x01")            # 1-dot inversion
        self._write(_DFUNCTR, b"\x80\x02\x3b")
        self._write(_DOCA, b"\x40\x8a\x00\x00\x29\x19\xa5\x33")

        self._write(_PWCTR2, b"\x06")
        self._write(_PWCTR3, b"\xa7")
        self._write(_VCMPCTL, b"\x18")
        time.sleep_ms(120)

        # Reference gamma for this panel. A picture appears without these, but
        # mid greys go muddy and the dim grey used for unselected axis labels
        # stops being distinguishable from the background.
        self._write(_GAMMAP, b"\xf0\x09\x0b\x06\x04\x15\x2f\x54"
                             b"\x42\x3c\x17\x14\x18\x1b")
        self._write(_GAMMAN, b"\xe0\x09\x0b\x06\x04\x03\x2b\x43"
                             b"\x42\x3b\x16\x14\x17\x1b")
        time.sleep_ms(120)

        # Close the extended set again. Leaving it open is not fatal, but a
        # stray byte landing on an extended register while it is unlocked can
        # reconfigure power or gamma mid-run.
        self._write(_CSCON, _LOCK_1)
        self._write(_CSCON, _LOCK_2)
        time.sleep_ms(120)

        self._write(_DISPON)
        time.sleep_ms(120)
