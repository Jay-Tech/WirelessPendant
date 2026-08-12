"""TCA9554 I2C I/O expander, enough of it to drive a reset line.

Exists because the ESP32-S3 display board puts the panel's reset on an expander
rather than a GPIO, so the display driver cannot simply toggle a pin. See
hardware/platform-decision.md for the board's pin map.

Deliberately small. This is not a general expander library - it drives outputs
and nothing else, because that is all the board asks for and a wider surface
would be untested code shipped on the strength of a datasheet.

Registers are shadowed rather than read back. The part on that board answers
writes and refuses reads, which a read-modify-write would trip over, and there
is no need for one: the reset state of the configuration register is known
(0xFF, every pin an input) so tracking it locally is exact. Only pins actually
asked for are switched to outputs; the rest stay high impedance, which is what
keeps this safe on a board where the other seven lines are somebody else's.
"""

_INPUT_PORT = 0x00
_OUTPUT_PORT = 0x01
_POLARITY = 0x02
_CONFIG = 0x03

# The part comes out of reset with every pin an input and the output latch high.
_CONFIG_RESET = 0xFF
_OUTPUT_RESET = 0xFF


class TCA9554:
    """Outputs on a TCA9554 / PCA9554 at `address`."""

    def __init__(self, i2c, address=0x20):
        self._i2c = i2c
        self._address = address
        self._config = _CONFIG_RESET
        self._output = _OUTPUT_RESET

    def _write(self, register, value):
        self._i2c.writeto(self._address, bytes([register, value & 0xFF]))

    def output(self, pin, value):
        """Drive `pin` (0-7) high or low, making it an output the first time.

        The output latch is set before the direction is, so a pin switching to
        an output starts at the level asked for rather than at whatever the
        latch happened to hold. On a reset line that difference is a spurious
        pulse into whatever is downstream.
        """
        mask = 1 << pin

        if value:
            self._output |= mask
        else:
            self._output &= ~mask
        self._write(_OUTPUT_PORT, self._output)

        if self._config & mask:
            self._config &= ~mask
            self._write(_CONFIG, self._config)

    def reset_line(self, pin, assert_ms=10, settle_ms=200):
        """Return a callable that pulses `pin` as an active-low reset.

        Shaped to hand straight to the display driver, which takes a callable
        rather than a pin number for exactly this case.

        The settle time matters more than it looks: an ST7796S will accept
        commands before it has finished its own internal reset and then ignore
        half of them, which reads as a driver bug rather than a timing one.
        Waveshare's own sequence for this board waits 200 ms.
        """
        def pulse():
            import time
            self.output(pin, 1)
            time.sleep_ms(assert_ms)
            self.output(pin, 0)
            time.sleep_ms(assert_ms)
            self.output(pin, 1)
            time.sleep_ms(settle_ms)
        return pulse
