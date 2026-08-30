"""Minimal ILI9341 driver - enough for a DRO, nothing more.

Deliberately not a general graphics library. A DRO needs solid rectangles and
text, redrawn in small pieces, so this exposes `fill_rect` and `blit` and
leaves layout to the caller.

Colour is RGB565, but stored **byte-swapped**, because the panel wants
big-endian while MicroPython's RGB565 framebuf is native little-endian. Keeping
colours pre-swapped means a framebuf can be pushed straight down SPI with no
per-pixel fixing up, which is the difference between a usable refresh rate and
a visibly slow one. Use `color565()` and the constants below and it stays
invisible.
"""

import time

import framebuf
from machine import Pin

# Commands used here. The ILI9341 has many more; these are the ones a DRO needs.
_SWRESET = const(0x01)
_SLPOUT = const(0x11)
_GAMMASET = const(0x26)
_DISPON = const(0x29)
_CASET = const(0x2A)
_PASET = const(0x2B)
_RAMWR = const(0x2C)
_MADCTL = const(0x36)
_PIXFMT = const(0x3A)
_FRMCTR1 = const(0xB1)
_DFUNCTR = const(0xB6)
_PWCTR1 = const(0xC0)
_PWCTR2 = const(0xC1)
_VMCTR1 = const(0xC5)
_VMCTR2 = const(0xC7)

# MADCTL: MY 0x80, MX 0x40, MV 0x20, BGR 0x08. These modules are BGR-wired, so
# omitting that bit swaps red and blue - a good first check if colours look odd.
_ROTATIONS = {
    0: (0x48, 240, 320),
    90: (0x28, 320, 240),
    180: (0x88, 240, 320),
    270: (0xE8, 320, 240),
}


def color565(r, g, b):
    """RGB to the panel's byte order. See the module note on byte swapping."""
    value = ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)
    return ((value & 0xFF) << 8) | (value >> 8)


def _no_chip_select(_value):
    """Stands in for a chip select the board does not have.

    Some boards tie the panel's select low permanently, so there is no pin to
    toggle. Swallowing the call here keeps every transfer site written the one
    way, rather than each of the six growing a test for whether the pin exists.
    """
    pass


BLACK = color565(0, 0, 0)
WHITE = color565(255, 255, 255)
GREY = color565(128, 128, 128)
DIM = color565(70, 70, 70)
RED = color565(255, 40, 40)
GREEN = color565(40, 220, 90)
AMBER = color565(255, 176, 0)
BLUE = color565(60, 140, 255)


class ILI9341:
    # Looked up through the instance so a panel with the same command set but a
    # different geometry can substitute its own. The MADCTL values are shared -
    # those bits mean the same thing on any MIPI DCS part - and only the
    # dimensions change.
    ROTATIONS = _ROTATIONS

    def __init__(self, spi, cs, dc, rst=None, backlight=None, rotation=90):
        """`cs` may be None on a board that ties chip select low, and `rst` may
        be a callable instead of a pin where reset is not a GPIO at all - both
        of which the ESP32-S3 display board is. Passing a callable keeps the
        expander out of this file: the driver asks for a reset and does not care
        whether that is a pin or an I2C write.
        """
        self._spi = spi
        self._cs = _no_chip_select if cs is None else Pin(cs, Pin.OUT, value=1)
        self._dc = Pin(dc, Pin.OUT, value=0)

        if callable(rst):
            self._reset_fn = rst
            self._rst = None
        else:
            self._reset_fn = None
            self._rst = Pin(rst, Pin.OUT, value=1) if rst is not None else None

        self._backlight_pin = backlight
        self._backlight = (Pin(backlight, Pin.OUT, value=1)
                           if backlight is not None else None)
        # Built on first use rather than here, so a board or a port without PWM
        # costs nothing and every existing caller of backlight() is unaffected.
        self._backlight_pwm = None
        self._pwm_unavailable = False

        madctl, self.width, self.height = self.ROTATIONS[rotation]

        # One reusable scratch row keeps fills from allocating per call, which
        # matters when the DRO is redrawing on every status frame.
        self._chunk = bytearray(512)

        self.reset()
        self._init_panel(madctl)

    # --- transport --------------------------------------------------------

    def _write(self, command, data=None):
        self._cs(0)
        self._dc(0)
        self._spi.write(bytes([command]))
        if data:
            self._dc(1)
            self._spi.write(data)
        self._cs(1)

    def reset(self):
        if self._reset_fn is not None:
            self._reset_fn()
            return
        if self._rst is None:
            return
        self._rst(1)
        time.sleep_ms(5)
        self._rst(0)
        time.sleep_ms(20)
        self._rst(1)
        time.sleep_ms(150)

    def _init_panel(self, madctl):
        self._write(_SWRESET)
        time.sleep_ms(150)
        # Power and VCOM values from the panel's reference init. Skipping these
        # usually still produces a picture, but a washed out or flickering one.
        self._write(_PWCTR1, b"\x23")
        self._write(_PWCTR2, b"\x10")
        self._write(_VMCTR1, b"\x3e\x28")
        self._write(_VMCTR2, b"\x86")
        self._write(_MADCTL, bytes([madctl]))
        self._write(_PIXFMT, b"\x55")          # 16 bits per pixel
        self._write(_FRMCTR1, b"\x00\x18")     # ~70 Hz
        self._write(_DFUNCTR, b"\x08\x82\x27")
        self._write(_GAMMASET, b"\x01")
        self._write(_SLPOUT)
        time.sleep_ms(120)
        self._write(_DISPON)
        time.sleep_ms(20)

    def backlight(self, on):
        self.set_brightness(1.0 if on else 0.0)

    def set_brightness(self, level):
        """Backlight from 0.0 to 1.0, by PWM where the port has it.

        The backlight is the largest single load on this device - 100-150 mA of
        it against 50-120 mA for the CPU and radio together - so turning it down
        while nobody is looking is most of what a pendant can do about its own
        battery. Turning it *off* would save slightly more and is not worth it:
        a dim DRO can still be read at a glance from the machine, and a blank
        one has to be woken before it can answer the question that prompted the
        glance.

        Falls back to on/off where PWM is unavailable, so the driver stays
        usable on any port. The threshold is deliberately low - anything the
        caller meant as "dimmed" is still meant as "lit".
        """
        if self._backlight is None:
            return

        level = min(max(level, 0.0), 1.0)

        if self._backlight_pwm is None and not self._pwm_unavailable:
            try:
                from machine import PWM
                # 1 kHz: fast enough that no eye or camera sees it flicker, and
                # clear of the few-hundred-hertz range where a backlight's own
                # inductor can be heard whining in a quiet shop.
                self._backlight_pwm = PWM(Pin(self._backlight_pin),
                                          freq=1000)
            except (ImportError, ValueError, TypeError, AttributeError):
                self._pwm_unavailable = True

        if self._backlight_pwm is not None:
            self._backlight_pwm.duty_u16(int(65535 * level))
        else:
            self._backlight(1 if level > 0.05 else 0)

    # --- drawing ----------------------------------------------------------

    def _window(self, x, y, w, h):
        x1, y1 = x + w - 1, y + h - 1
        self._write(_CASET, bytes([x >> 8, x & 0xFF, x1 >> 8, x1 & 0xFF]))
        self._write(_PASET, bytes([y >> 8, y & 0xFF, y1 >> 8, y1 & 0xFF]))
        self._write(_RAMWR)

    def fill_rect(self, x, y, w, h, color):
        if w <= 0 or h <= 0:
            return
        x = max(0, min(x, self.width - 1))
        y = max(0, min(y, self.height - 1))
        w = min(w, self.width - x)
        h = min(h, self.height - y)

        self._window(x, y, w, h)

        # Fill the scratch buffer once, then repeat it, rather than building a
        # buffer the size of the rectangle.
        low, high = color & 0xFF, color >> 8
        chunk = self._chunk
        for i in range(0, len(chunk), 2):
            chunk[i] = low
            chunk[i + 1] = high

        remaining = w * h * 2
        self._cs(0)
        self._dc(1)
        while remaining >= len(chunk):
            self._spi.write(chunk)
            remaining -= len(chunk)
        if remaining:
            self._spi.write(memoryview(chunk)[:remaining])
        self._cs(1)

    def fill(self, color):
        self.fill_rect(0, 0, self.width, self.height, color)

    def blit(self, buffer, x, y, w, h):
        """Push a pre-rendered RGB565 buffer (already byte-swapped)."""
        self._window(x, y, w, h)
        self._cs(0)
        self._dc(1)
        self._spi.write(buffer)
        self._cs(1)


class Glyphs:
    """Renders the built-in 8x8 font scaled up, one character at a time.

    Per-character rather than per-string so a caller can repaint only the
    digits that changed - a DRO redrawing a whole line every status frame is
    both slow and visibly flickery.

    Scaling uses framebuf.fill_rect per source pixel, which runs in C. Writing
    the scaled pixels individually from Python is roughly an order of magnitude
    slower and turns a 10 Hz DRO into a visible crawl.
    """

    def __init__(self, scale=3):
        self.scale = scale
        self.width = 8 * scale
        self.height = 8 * scale
        self._cell = bytearray(self.width * self.height * 2)
        self._fb = framebuf.FrameBuffer(
            self._cell, self.width, self.height, framebuf.RGB565)
        self._glyph_buf = bytearray(8)
        self._glyph = framebuf.FrameBuffer(
            self._glyph_buf, 8, 8, framebuf.MONO_HLSB)

    def render(self, char, color, background):
        self._glyph.fill(0)
        self._glyph.text(char, 0, 0, 1)

        self._fb.fill(background)
        scale = self.scale
        for row in range(8):
            for col in range(8):
                if self._glyph.pixel(col, row):
                    self._fb.fill_rect(col * scale, row * scale,
                                       scale, scale, color)
        return self._cell
