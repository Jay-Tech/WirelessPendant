"""AXP2101 PMIC, enough of it to read the battery.

The ESP32-S3-Touch-LCD-3.5 puts charging, power path, fuel gauge and the power
button on an AXP2101 at 0x34 on the shared I2C bus. See
hardware/platform-decision.md - moving the power subsystem onto the board was
half the reason for choosing it.

Deliberately read-only about power. This part also owns every rail on the
board, including the one feeding the panel and the one feeding the CPU it is
being talked to from, so a stray write here is a brown-out rather than a bug
you get to see. The only registers written are three enable bits - battery
detection, the fuel gauge, and the battery voltage ADC channel - each by
read-modify-write, each documented below. Nothing sets a voltage, a current or
an output enable, and nothing should be added here that does without a reason
better than completeness.

Register numbers follow XPowersLib, which is the de facto map for this part.
`probe_axp2101.py` confirms them against a board rather than leaving them on
the strength of a document: the chip id is checked, and the status bits are
watched while USB is plugged and unplugged, which is the only way to tell
"battery present" from "USB present" without guessing.
"""

# Identity. Read first, because every register below means something different
# on a part that is not this one, and the bus has six devices on it.
_REG_CHIP_ID = 0x03
CHIP_ID = 0x4A

# Status. Verified on the board by removing the cell and diffing the register
# dump either side of it - probe_axp2101_map.py, 2026-08-15:
#
#   STATUS1  00111000 -> 00100000     bits 3 and 4 cleared, bit 5 held
#   STATUS2  00110010 -> 00010101     charge field 01 -> 00, state 010 -> 101
#   VBAT     4.019 V  -> 0.042 V
#
# Bit 3 followed the cell and bit 5 did not, with USB untouched throughout,
# which is what identifies them as battery-present and vbus rather than the
# other way round. Bit 4 is the BATFET, which opens when the cell goes.
_REG_STATUS1 = 0x00         # vbus good, batfet, battery present
_REG_STATUS2 = 0x01         # charging direction and charger state

_STATUS1_VBUS_GOOD = 5
_STATUS1_BATTERY_PRESENT = 3

# Charge direction lives in bits 6:5 of STATUS2: 00 standby, 01 charging,
# 10 discharging. Read as a two-bit field rather than as one bit, so
# "not charging" and "discharging" stay distinguishable - on a tool that sits
# on a charger overnight, "standby because it is full" and "running the cell
# down" are the two states worth telling apart.
_STATUS2_CHARGE_SHIFT = 5
_STATUS2_CHARGE_MASK = 0x03
CHARGE_STANDBY = 0
CHARGE_CHARGING = 1
CHARGE_DISCHARGING = 2

# Common configuration. Bit 3 enables the fuel gauge, which is what makes the
# percentage register mean anything.
_REG_COMMON_CONFIG = 0x18
_COMMON_GAUGE_ENABLE = 3

# ADC channel enables. Bit 0 is the battery voltage channel. Without it the
# data registers below hold whatever they last held, which reads as a plausible
# constant rather than as an error - the failure this class exists to avoid.
_REG_ADC_CHANNEL = 0x30
_ADC_BATTERY_VOLTAGE = 0

# Battery voltage, in millivolts, across two registers. The high byte carries
# five significant bits; the rest are not voltage and are masked off. A cell
# cannot reach the 8191 mV that leaves, so the mask width is not load-bearing -
# it is there so a set flag bit above the value cannot be read as 20 volts.
_REG_VBAT_H = 0x34
_REG_VBAT_L = 0x35
_VBAT_H_MASK = 0x1F

# Battery detection. Off, the percentage and the present bit both stay put.
_REG_BATTERY_DETECT = 0x68
_BATTERY_DETECT_ENABLE = 0

# The gauge's own state of charge, 0-100, one register.
#
# **Measured unreliable on this board, 2026-08-15.** Across four runs with the
# cell untouched and sitting at 4.01-4.02 V on charge, this register read 81,
# then decayed 82 -> 59 over thirty seconds, then read 31 twice. A cell at
# 4.0 V is genuinely around 75-85%, so 81 was right and 31 is not, and a
# 23-point slide in half a minute while charging is not a thing a battery can
# do. It reads 0 correctly with no cell, so it is not simply dead.
#
# The failure mode is the dangerous kind: every individual reading looks
# plausible, so nothing about the number announces that it is wrong. Prefer
# the voltage until this is understood - it has matched a meter to 13 mV and
# reads identically split and burst, and hardware/carrier-board.md already
# specifies thresholds in volts (warn near 3.4, shut down near 3.2).
_REG_BATTERY_PERCENT = 0xA4


class AXP2101:
    """Battery readings from an AXP2101 at `addr`."""

    def __init__(self, i2c, addr=0x34):
        self._i2c = i2c
        self._addr = addr

    # --- bus ---------------------------------------------------------------

    def _read(self, register):
        return self._i2c.readfrom_mem(self._addr, register, 1)[0]

    def _write(self, register, value):
        self._i2c.writeto_mem(self._addr, register, bytes([value & 0xFF]))

    def _set_bit(self, register, bit):
        """Set one bit, leaving the rest of the register as found.

        Read-modify-write rather than a shadowed value, unlike the TCA9554 on
        this same bus. That part refuses reads so it has to be tracked; this
        one answers them, and the registers touched here carry other people's
        settings - the charger's and the board's - which must survive.
        """
        value = self._read(register)
        if value & (1 << bit):
            return
        self._write(register, value | (1 << bit))

    def _get_bit(self, register, bit):
        return bool(self._read(register) & (1 << bit))

    # --- identity ----------------------------------------------------------

    def chip_id(self):
        return self._read(_REG_CHIP_ID)

    def present(self):
        """Is there an AXP2101 answering, and is it this part?

        Address alone is not enough. Something acknowledging 0x34 on a board
        that is not the expected one would otherwise be read as a battery
        reporting nonsense, and a pendant showing a wrong charge is worse than
        one showing none.
        """
        try:
            return self.chip_id() == CHIP_ID
        except OSError:
            return False

    # --- bring-up ----------------------------------------------------------

    def begin(self):
        """Turn on the three things a battery reading depends on.

        Separate from the constructor because it writes, and construction that
        writes to a PMIC is not something to do as a side effect of an import
        landing in the wrong order.
        """
        self.enable_battery_detect()
        self.enable_gauge()
        self.enable_battery_voltage_adc()

    def enable_battery_detect(self):
        self._set_bit(_REG_BATTERY_DETECT, _BATTERY_DETECT_ENABLE)

    def enable_gauge(self):
        self._set_bit(_REG_COMMON_CONFIG, _COMMON_GAUGE_ENABLE)

    def enable_battery_voltage_adc(self):
        self._set_bit(_REG_ADC_CHANNEL, _ADC_BATTERY_VOLTAGE)

    # --- readings ----------------------------------------------------------

    def is_battery_connected(self):
        return self._get_bit(_REG_STATUS1, _STATUS1_BATTERY_PRESENT)

    def is_vbus_good(self):
        """Is USB supplying power? True while charging and while sitting full."""
        return self._get_bit(_REG_STATUS1, _STATUS1_VBUS_GOOD)

    def charge_state(self):
        """CHARGE_STANDBY, CHARGE_CHARGING or CHARGE_DISCHARGING."""
        status = self._read(_REG_STATUS2)
        return (status >> _STATUS2_CHARGE_SHIFT) & _STATUS2_CHARGE_MASK

    def is_charging(self):
        return self.charge_state() == CHARGE_CHARGING

    def get_battery_voltage(self):
        """Cell voltage in millivolts, or 0 with no battery.

        Zero rather than None for the no-battery case, matching the reference
        library, and harmless here because the caller checks presence first.
        """
        if not self.is_battery_connected():
            return 0
        high = self._read(_REG_VBAT_H) & _VBAT_H_MASK
        return (high << 8) | self._read(_REG_VBAT_L)

    def get_battery_percentage(self):
        """State of charge 0-100, or None if the gauge has no answer.

        The register reads 0xFF while the gauge is still settling after a cell
        is connected. Reporting that as 255% would be obvious; reporting it as
        a clamped 100% would not, and would show a flat pendant as full.
        """
        if not self.is_battery_connected():
            return None
        percent = self._read(_REG_BATTERY_PERCENT)
        if percent > 100:
            return None
        return percent
