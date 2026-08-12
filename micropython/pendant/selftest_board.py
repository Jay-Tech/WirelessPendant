"""Is this board what the pendant expects? Ten seconds, no wiring, no hands.

For a board out of the box, before anything is soldered to it. Checks the
firmware has what the pendant needs and that every device the pendant talks to
is present and answering, so a bring-up problem later is a wiring problem
rather than a "was the right image even flashed" problem.

    python -m mpremote connect COM17 run micropython/pendant/selftest_board.py

Deliberately does not test the encoder or the buttons: those need a hand on the
wheel and a finger on a switch, and belong in selftest_pcnt.py and the live
monitor. This is the part that can be answered without either.

Targets the Waveshare ESP32-S3-Touch-LCD-3.5. Pin map and the reasoning behind
it are in hardware/platform-decision.md.
"""

import sys
import gc

# --- what this board is supposed to look like -----------------------------

I2C_SCL, I2C_SDA = 7, 8

LCD_MOSI, LCD_MISO, LCD_SCLK = 1, 2, 5
LCD_DC, LCD_BL = 3, 6
LCD_CS = None                       # tied low on this board
TCA_ADDRESS, TCA_LCD_RESET = 0x20, 1

# Every one of these answers on the shared bus. A missing entry means a board
# that is not the one this firmware was written for; an unexpected extra is
# worth knowing about rather than ignoring.
EXPECTED_I2C = {
    0x18: "ES8311 audio codec",
    0x20: "TCA9554 I/O expander (LCD reset)",
    0x34: "AXP2101 PMIC",
    0x38: "FT6336U touch",
    0x51: "PCF85063 RTC",
    0x6B: "QMI8658 IMU",
}

FT6336U_CHIP_ID, FT6336U_VENDOR_ID = 0x64, 0x11

failures = []


def check(label, ok, detail=""):
    print("  {:<44} {}{}".format(
        label, "PASS" if ok else "FAIL", "  " + detail if detail else ""))
    if not ok:
        failures.append(label)


# --- firmware -------------------------------------------------------------

def check_firmware():
    print("\nfirmware")
    impl = sys.implementation
    print("  {:<44} {}".format("build", impl._build))
    print("  {:<44} {}".format("version", ".".join(str(v) for v in impl.version[:3])))

    gc.collect()
    free = gc.mem_free()
    # Internal SRAM alone is a few hundred KB. Megabytes means the octal PSRAM
    # image was flashed and the external RAM came up - which is what makes a
    # full 320x480x2 framebuffer (307 KB) affordable.
    check("PSRAM active", free > 1_000_000,
          "{} bytes free".format(free))

    try:
        import espnow
        espnow.ESPNow()
        check("espnow available", True)
    except Exception as exc:
        check("espnow available", False, repr(exc))


# --- the shared I2C bus ---------------------------------------------------

def check_i2c(i2c):
    print("\nI2C bus on GPIO{}/{}".format(I2C_SCL, I2C_SDA))
    found = set(i2c.scan())

    for address in sorted(EXPECTED_I2C):
        check("{:#04x}  {}".format(address, EXPECTED_I2C[address]),
              address in found)

    for address in sorted(found - set(EXPECTED_I2C)):
        print("  {:<44} {}".format("{:#04x}  unexpected".format(address), "note"))

    # Identity, not just presence. A device that acknowledges its address but
    # reports the wrong part is a different panel with the same wiring.
    if 0x38 in found:
        try:
            chip = i2c.readfrom_mem(0x38, 0xA3, 1)[0]
            vendor = i2c.readfrom_mem(0x38, 0xA8, 1)[0]
            check("touch identifies as FT6336U",
                  chip == FT6336U_CHIP_ID and vendor == FT6336U_VENDOR_ID,
                  "chip {:#04x} vendor {:#04x}".format(chip, vendor))
        except Exception as exc:
            check("touch identifies as FT6336U", False, repr(exc))


# --- display --------------------------------------------------------------

def check_display(i2c):
    print("\ndisplay")
    try:
        from machine import Pin, SPI
        from tca9554 import TCA9554
        from st7796 import ST7796S
        from ili9341 import color565

        tca = TCA9554(i2c, TCA_ADDRESS)
        # The expander answers writes and refuses reads, so reaching it at all
        # is the only confirmation available - and a reset that throws here is
        # the difference between a dead panel and a dead bus.
        reset = tca.reset_line(TCA_LCD_RESET)

        spi = SPI(2, baudrate=20_000_000,
                  sck=Pin(LCD_SCLK), mosi=Pin(LCD_MOSI), miso=Pin(LCD_MISO))
        lcd = ST7796S(spi, cs=LCD_CS, dc=LCD_DC, rst=reset,
                      backlight=LCD_BL, rotation=0)

        check("panel initialises", True,
              "{}x{}".format(lcd.width, lcd.height))
        check("geometry is 320x480 portrait",
              lcd.width == 320 and lcd.height == 480)

        # Corners rather than a flat fill: a flat fill proves the bus works,
        # corners prove the rotation and the address window agree with it.
        lcd.fill(0)
        s = 60
        lcd.fill_rect(0, 0, s, s, color565(255, 255, 255))
        lcd.fill_rect(lcd.width - s, 0, s, s, color565(255, 0, 0))
        lcd.fill_rect(0, lcd.height - s, s, s, color565(0, 255, 0))
        lcd.fill_rect(lcd.width - s, lcd.height - s, s, s, color565(0, 0, 255))
        print("  {:<44} {}".format("look at the panel", "eyes"))
        print("      white top-left, red top-right,")
        print("      green bottom-left, blue bottom-right,")
        print("      with the USB-C port at the bottom.")
    except Exception as exc:
        check("panel initialises", False, repr(exc))


def main():
    print("\nboard self-test")
    print("=" * 52)

    check_firmware()

    from machine import I2C, Pin
    i2c = I2C(0, scl=Pin(I2C_SCL), sda=Pin(I2C_SDA), freq=400_000)
    check_i2c(i2c)
    check_display(i2c)

    print()
    if failures:
        print("{} FAILED: {}".format(len(failures), ", ".join(failures)))
        return 1
    print("board checks passed - wiring next: encoder on GPIO9/10, buttons on")
    print("GPIO38/39/40, then selftest_pcnt.py and the live monitor.")
    return 0


raise SystemExit(main())
