"""Battery readings for the pendant, or nothing, without ever being the reason
the pendant does not start.

Everything optional on this device follows the same rule - the touch panel, the
display - because a handheld that refuses to jog the machine over a charge
indicator has the priorities backwards. Construction never throws; `present`
says whether there is anything to read.
"""

from machine import I2C, Pin

try:
    from axp2101 import AXP2101
except ImportError:
    from pendant.axp2101 import AXP2101


# Open-circuit voltage against state of charge for a single lithium-polymer
# cell, in millivolts, highest first. Interpolated between points.
#
# Used instead of the AXP2101's own fuel gauge, which was measured wrong on
# this board - 81% and 31% at the same 4.01 V, and a 23-point slide in thirty
# seconds while charging. See the note on _REG_BATTERY_PERCENT in axp2101.py.
# The gauge is still readable through `gauge_percent()` for comparison, and if
# it is ever fixed this table is what it replaces.
#
# **A curve is a worse instrument than a gauge, and knowing how is the point.**
# It reads high while charging, because the charger holds the cell above its
# resting voltage - which is why `get_status` reports charging separately
# rather than folding it in. It reads low during a burst of radio traffic, for
# the few hundred milliseconds the cell sags under the load. And the middle of
# the curve is nearly flat, so between 3.7 and 3.9 V a few millivolts of error
# moves the answer by ten points. It is honest about the ends, which is where
# a warning has to be right, and approximate in the middle, where it does not.
_CURVE = (
    (4200, 100), (4150, 95), (4110, 90), (4080, 85), (4020, 80),
    (3980, 75), (3950, 70), (3910, 65), (3870, 60), (3850, 55),
    (3840, 50), (3820, 45), (3800, 40), (3790, 35), (3770, 30),
    (3750, 25), (3730, 20), (3710, 15), (3690, 10), (3610, 5),
    (3270, 0),
)


def percent_from_voltage(millivolts):
    """State of charge 0-100 for a cell at `millivolts`, or None below range.

    None rather than 0 under 3.27 V. A cell that low is either flat past the
    point the curve describes or not a cell at all - the empty connector on
    this board reads 42 mV - and both deserve "--" on the panel rather than a
    confident zero.
    """
    if millivolts is None or millivolts < _CURVE[-1][0]:
        return None
    if millivolts >= _CURVE[0][0]:
        return 100

    for index in range(len(_CURVE) - 1):
        high_mv, high_pct = _CURVE[index]
        low_mv, low_pct = _CURVE[index + 1]
        if millivolts >= low_mv:
            span = high_mv - low_mv
            if span <= 0:
                return low_pct
            return int(low_pct
                       + (high_pct - low_pct) * (millivolts - low_mv) / span)
    return 0


class BatteryMonitor:

    def __init__(self, scl_pin=7, sda_pin=8, i2c_bus=0, addr=0x34, i2c=None):
        self.present = False
        self.pmu = None
        try:
            # Opens the bus the touch panel and the expander also open, at the
            # same frequency, which start_display() already documents as the
            # convention here rather than an oversight.
            self.i2c = i2c or I2C(i2c_bus, scl=Pin(scl_pin), sda=Pin(sda_pin),
                                  freq=400000)
            pmu = AXP2101(self.i2c, addr=addr)
            if not pmu.present():
                return
            # Detection alone is not enough, and this is the part that was
            # wrong first time: with the ADC channel disabled the voltage
            # registers still read, and still return a steady plausible
            # number - the last conversion, or nothing at all. A reading that
            # is merely stale looks exactly like a reading that is right.
            pmu.begin()
            self.pmu = pmu
            self.present = True
        except Exception:
            # Any I2C fault at start-up means no battery reporting, not a dead
            # pendant. The bus is shared, so a fault here is as likely to be
            # somebody else's device holding the line as it is to be the PMIC.
            self.pmu = None
            self.present = False

    def get_status(self):
        """Returns (volts, percent, is_charging). Volts and percent may be None.

        `percent` comes from the voltage through `percent_from_voltage`, not
        from the PMIC's fuel gauge - see the note on `_CURVE`. It is None
        whenever there is nothing trustworthy to say: no PMIC, no cell, a bus
        error, or a voltage below the curve. Callers must show the gap rather
        than substituting a zero, which would read as flat rather than as
        unknown.
        """
        if self.pmu is None:
            return None, None, False

        try:
            if not self.pmu.is_battery_connected():
                return None, None, False

            millivolts = self.pmu.get_battery_voltage()
            return (millivolts / 1000.0 if millivolts else None,
                    percent_from_voltage(millivolts),
                    self.pmu.is_charging())
        except OSError:
            # A transient bus error is one lost sample. The caller polls on a
            # timer, so the next one will say.
            return None, None, False

    def gauge_percent(self):
        """The PMIC's own state of charge, for comparison with the curve.

        Not what the panel shows. Here so the two can be logged side by side -
        the gauge being wrong is a measurement on one board, and if it turns
        out to behave elsewhere this is what will show that.
        """
        if self.pmu is None:
            return None
        try:
            return self.pmu.get_battery_percentage()
        except OSError:
            return None
