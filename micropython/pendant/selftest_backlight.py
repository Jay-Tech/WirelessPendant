"""Does the backlight dim on this board, and by PWM or only on/off?

The idle dimming in pendant.py is worth exactly as much as this answers. If the
port has no PWM on the backlight pin the driver silently falls back to on/off,
which still works but turns "dim after two minutes" into "blank after two
minutes" - a different feature, and one worth knowing you have shipped.

    python tools/on_board.py micropython/pendant/selftest_backlight.py

Watch the panel. It steps down through six levels, holds at the dim level the
pendant actually uses, then comes back up. Non-interactive - nothing to catch.
"""

import time
from machine import Pin, SPI

try:
    from st7796 import ST7796S
    from tca9554 import TCA9554
    from ili9341 import color565
except ImportError:
    from pendant.st7796 import ST7796S
    from pendant.tca9554 import TCA9554
    from pendant.ili9341 import color565

I2C_SCL, I2C_SDA = 7, 8
LCD_MOSI, LCD_MISO, LCD_SCLK = 1, 2, 5
LCD_DC, LCD_BL = 3, 6
LCD_CS = None
TCA_ADDRESS, TCA_LCD_RESET = 0x20, 1

# Matches DISPLAY_DIM_LEVEL in pendant.py. If they drift apart this test stops
# describing the thing it is testing.
DIM_LEVEL = 0.15

STEPS = (1.0, 0.75, 0.5, 0.3, DIM_LEVEL, 0.05)
HOLD_MS = 1200


def main():
    print("\nbacklight self-test")
    print("=" * 56)

    from machine import I2C
    i2c = I2C(0, scl=Pin(I2C_SCL), sda=Pin(I2C_SDA), freq=400_000)
    tca = TCA9554(i2c, TCA_ADDRESS)

    spi = SPI(2, baudrate=20_000_000,
              sck=Pin(LCD_SCLK), mosi=Pin(LCD_MOSI), miso=Pin(LCD_MISO))
    lcd = ST7796S(spi, cs=LCD_CS, dc=LCD_DC,
                  rst=tca.reset_line(TCA_LCD_RESET),
                  backlight=LCD_BL, rotation=0)

    # Something bright and uniform, so the steps are obvious. A dark screen
    # dims convincingly whether or not the backlight is doing anything.
    lcd.fill(color565(255, 255, 255))

    lcd.set_brightness(1.0)
    has_pwm = lcd._backlight_pwm is not None

    print("  {:<40} {}".format(
        "backlight control",
        "PWM on GP{}".format(LCD_BL) if has_pwm else "on/off only"))
    if not has_pwm:
        print("      The driver fell back. Idle dimming will blank the panel")
        print("      rather than dim it - see set_brightness in ili9341.py.")

    print("\n  stepping down, {} ms per level:".format(HOLD_MS))
    for level in STEPS:
        print("      {:>5.0f}%{}".format(
            level * 100,
            "   <- the level the pendant idles at" if level == DIM_LEVEL
            else ""))
        lcd.set_brightness(level)
        time.sleep_ms(HOLD_MS)

    print("\n  back to full")
    lcd.set_brightness(1.0)
    time.sleep_ms(HOLD_MS)

    print("\n  What should have happened: six clearly different brightnesses,")
    print("  smoothly separated, ending back at full. If the panel only")
    print("  blinked between lit and dark, PWM is not reaching the pin")
    print("  whatever the line above says.")

    return 0 if has_pwm else 1


raise SystemExit(main())
