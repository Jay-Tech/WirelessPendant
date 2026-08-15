"""Does the battery chain work on this board, end to end?

Exercises what the pendant actually calls - BatteryMonitor, which sits on
axp2101.py, whose reading goes through the voltage curve - rather than the
registers underneath it. probe_axp2101.py covers those; this covers the path
from the cell to the number that reaches the panel.

    python tools/on_board.py micropython/pendant/selftest_battery.py

Reports the curve's answer beside the PMIC's own gauge every time. They
disagreed badly on the bench board - 31% by gauge against 80% by curve at
4.02 V - and that comparison is the record of whether that holds.

Non-interactive, like everything else here: `mpremote` output does not reach
the operator until the run ends, so nothing asks for a hand mid-run. To see
the no-cell path, unplug the battery and run it again.
"""

try:
    from battery_monitor import BatteryMonitor, percent_from_voltage
except ImportError:
    from pendant.battery_monitor import BatteryMonitor, percent_from_voltage

failures = []


def check(label, ok, detail=""):
    print("  {:<44} {}{}".format(
        label, "PASS" if ok else "FAIL", "  " + detail if detail else ""))
    if not ok:
        failures.append(label)


def main():
    print("\nbattery self-test")
    print("=" * 60)

    monitor = BatteryMonitor()
    check("constructing never throws", True)
    check("PMIC answers", monitor.present,
          "AXP2101 at 0x34" if monitor.present else "none found")

    if not monitor.present:
        print("\n  No PMIC. On this board that is a fault; on a board without")
        print("  one the pendant runs and shows '--', which is the point of")
        print("  BatteryMonitor never raising.")
        return 1

    volts, percent, charging = monitor.get_status()
    gauge = monitor.gauge_percent()

    print("\nreading")
    if volts is None:
        print("  {:<44} {}".format("cell", "not fitted"))
        check("no cell reports no percentage", percent is None)
        check("  and the panel would show a gap", percent is None, '"--"')
        print("\n  Unplugged. Plug the cell in and run again for the rest.")
        return 1 if failures else 0

    print("  {:<44} {:.3f} V".format("cell", volts))
    print("  {:<44} {}".format(
        "by curve", "{}%".format(percent) if percent is not None
        else "below the curve"))
    print("  {:<44} {}".format(
        "by PMIC gauge", "{}%".format(gauge) if gauge is not None
        else "no answer"))
    print("  {:<44} {}".format("charging", "yes" if charging else "no"))

    # A cell the PMIC says is present must be inside the range a lithium cell
    # can occupy. Outside it, something upstream is wrong - the wrong register,
    # the wrong mask, or the 2.52 V reading that has been seen once and is not
    # understood.
    check("\n  voltage is credible for a lithium cell",
          2.9 <= volts <= 4.35, "{:.3f} V".format(volts))
    check("  the curve has an answer for it", percent is not None)

    if percent is not None:
        check("  and it agrees with the voltage",
              percent == percent_from_voltage(int(volts * 1000)))

    if gauge is not None and percent is not None:
        spread = abs(gauge - percent)
        # Not a failure. The gauge was wrong on the bench board and the curve
        # is what the panel shows either way, so this is a record rather than a
        # verdict - a board where they agree is worth knowing about too.
        print("\n  {:<44} {} points".format("curve against gauge", spread))
        if spread > 20:
            print("      They disagree, as they did on the bench board.")
            print("      The curve is what the panel shows. See _CURVE.")
        else:
            print("      They agree here, unlike on the bench board.")

    print()
    if failures:
        print("{} FAILED: {}".format(len(failures), ", ".join(failures)))
        return 1
    print("battery chain works end to end")
    return 0


raise SystemExit(main())
