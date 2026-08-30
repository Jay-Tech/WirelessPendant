"""Dump the AXP2101's registers, once, so two dumps can be compared.

Written because the obvious answer needed proving. `probe_axp2101.py` reads a
plausible voltage and percentage, but "plausible" is how a pendant ends up
reporting 82% into an empty connector. The only way to identify which bit
reports the battery is to change the battery and see which bit follows.

**One shot, no waiting, no prompts.** Three earlier attempts asked the operator
to unplug the cell partway through a timed window, and all three failed the
same way: this board is driven through `mpremote`, whose output does not reach
the operator until the run finishes, so there is no moment during a run when
they can see a prompt and act on it. Anything asking for a hand mid-run is
asking for something the harness cannot deliver.

So the state change happens *between* runs, where there is no clock at all:

    python tools/on_board.py micropython/pendant/probe_axp2101_map.py   # cell in
    (unplug the battery JST at leisure)
    python tools/on_board.py micropython/pendant/probe_axp2101_map.py   # cell out

and the two dumps are diffed afterwards. Registers that differ are the ones
that track the cell.

READS ONLY. Nothing here writes, including the enable bits `probe_axp2101.py`
sets, because the question is what the part reports and a write is a way to
change the answer without noticing.
"""

from machine import I2C, Pin

I2C_SCL, I2C_SDA = 7, 8
ADDRESS = 0x34

# Blocks worth reading. Not the whole 0x00-0xFF space: unimplemented addresses
# on this part either NAK or return the last byte on the bus, and a page of
# either would bury the few registers that matter.
BLOCKS = (
    (0x00, 0x0A, "status and IRQ"),
    (0x10, 0x1B, "common config, gauge, watchdog"),
    (0x20, 0x28, "power-on/off source"),
    (0x30, 0x3A, "ADC control and data"),
    (0x40, 0x4B, "IRQ enable and status"),
    (0x60, 0x70, "charger and battery detection"),
    (0xA0, 0xA6, "fuel gauge"),
)


def read_all(i2c):
    """{register: value} for every address that answers."""
    values = {}
    for start, end, _ in BLOCKS:
        for register in range(start, end):
            try:
                values[register] = i2c.readfrom_mem(ADDRESS, register, 1)[0]
            except OSError:
                pass
    return values


def main():
    print("\nAXP2101 register dump")
    print("=" * 64)

    i2c = I2C(0, scl=Pin(I2C_SCL), sda=Pin(I2C_SDA), freq=400_000)
    if ADDRESS not in i2c.scan():
        print("  nothing at {:#04x} - wrong board or dead bus".format(ADDRESS))
        return 1

    values = read_all(i2c)

    for start, end, name in BLOCKS:
        line = ["{:02x}={:02x}".format(r, values[r])
                for r in range(start, end) if r in values]
        if line:
            print("  {:<34} {}".format(name, " ".join(line)))

    # The two readings that carry the answer, decoded, so a dump can be read
    # at a glance without doing the arithmetic by hand every time.
    #
    # VBAT read as one two-byte burst as well as as two separate reads. One run
    # reported a steady 2.52 V on a cell nobody had touched, bracketed by runs
    # reading 4.01 V, and a value updating between the high byte and the low
    # byte would explain it. If the two agree, the split read is exonerated and
    # the cause is upstream of the ADC.
    split_h = values.get(0x34, 0) & 0x1F
    split = (split_h << 8) | values.get(0x35, 0)
    burst_data = i2c.readfrom_mem(ADDRESS, 0x34, 2)
    burst = ((burst_data[0] & 0x1F) << 8) | burst_data[1]

    status1 = values.get(0x00, 0)
    status2 = values.get(0x01, 0)
    percent = values.get(0xA4, 0xFF)

    print("\n  {:<20} {:.3f} V".format("VBAT, split reads", split / 1000.0))
    print("  {:<20} {:.3f} V   {}".format(
        "VBAT, one burst", burst / 1000.0,
        "agree" if abs(burst - split) < 30 else "DISAGREE - torn read"))
    print("  {:<20} {}".format(
        "gauge", "{}%".format(percent) if percent <= 100 else "no answer"))
    print("  {:<20} {:08b}".format("STATUS1", status1))
    print("  {:<20} {:08b}   charge field {:02b}, charger state {:03b}".format(
        "STATUS2", status2, (status2 >> 5) & 0x03, status2 & 0x07))

    return 0


raise SystemExit(main())
