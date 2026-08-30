"""Measure what the encoder actually puts out, using the ADC as a scope.

A multimeter cannot answer this. One detent of a 100 PPR wheel is one full
quadrature cycle, so both channels return to their resting state at every
click - catching the high level means holding the wheel between detents, and a
DVM on a moving signal reads the average rather than either level. Sampling
fast and reporting the two clusters gives the real answer.

**On the ESP32-S3 there is nothing to wire.** GPIO1-10 are all ADC1 channels,
so the encoder's own pins - GP9 and GP10 - are read in place.

On the Pico, GP2/GP3 are not ADC capable, so the DIVIDER OUTPUTS (the
junctions, not the encoder pins) have to be jumpered across:

    channel A junction  ->  GP26
    channel B junction  ->  GP27

That is safe by construction: a 10k/20k divider on a 4.9 V supply cannot
exceed 3.27 V, well inside the ADC's range. Do not connect the encoder pins
directly there - that is the thing we are trying to find out about.

**Set SUPPLY_V to whatever the encoder is actually running from**, and mind the
order when changing it. A 5 V encoder with the 22k removed puts 5 V on a pin
that is not 5 V tolerant, on either board. Move the supply to 3V3 first, then
take the resistors out.

Run it, then turn the handwheel steadily for the whole sampling window:

    python tools/on_board.py micropython/pendant/probe_encoder.py
"""

import time
import sys
from machine import ADC, Pin

_ESP32 = sys.platform == "esp32"

SAMPLE_SECONDS = 6
ADC_FULL_SCALE = 65535
ADC_VREF = 3.3

# The divider that is physically installed, so the tool can report the encoder
# side of it rather than just its own input.
#
# For an open-collector encoder the internal pull-up is already the top leg, so
# the external circuit is a single resistor to ground and R_TOP_K is 0 - the
# ADC then reads the encoder output directly and the ratio is 1.
#
#   characterising an unknown encoder:  10.0 / 20.0   (external divider)
#   verifying the open-collector fix:    0.0 / 22.0   (single resistor)
#   the 3V3 trial:                        0.0 /  0.0   (nothing external)
#
# Both zero means no external network at all - the ADC is on the encoder's
# output and the ratio is 1. Spelled out rather than left as 0/22, which gives
# the same ratio arithmetically while printing a resistor that is not on the
# bench.
R_TOP_K = 0.0
R_BOTTOM_K = 0.0

# On the ESP32-S3 the encoder's own pins are ADC channels - GPIO1-10 are all
# ADC1 - so there is nothing to wire. On the Pico, GP2/GP3 are not ADC capable
# and the divider junctions have to be jumpered across to GP26/GP27, which is
# what the header above describes.
CHANNELS = (("A", 9), ("B", 10)) if _ESP32 else (("A", 26), ("B", 27))

# What the encoder is actually being powered from, measured rather than
# assumed. Every conclusion below is relative to it: the same 3.06 V at the pin
# means "healthy open-collector" on a 5 V supply and "barely switching" on a
# 3.3 V one, so a stale value here inverts the answer rather than blurring it.
#
#   the shipped build:      4.909  (5 V rail, 22k to ground fitted)
#   the 3V3 trial:          3.3    (encoder on 3V3, 22k REMOVED)
SUPPLY_V = 3.3

# Input-high threshold of the part doing the reading. The ESP32-S3 wants
# 0.75 x VDD, appreciably higher than the RP2350's, and a level that was
# comfortable on the Pico can sit under it here.
INPUT_HIGH_V = 2.48 if _ESP32 else 2.15

# Levels are sorted into buckets and the two densest clusters reported. 40
# buckets over 3.3 V is ~82 mV of resolution, fine enough to separate a logic
# low from a logic high and coarse enough that noise does not fragment them.
BUCKETS = 40


def divider_ratio():
    total = R_TOP_K + R_BOTTOM_K
    if not total:
        return 1.0
    return R_BOTTOM_K / total


def sample(adcs, seconds):
    """Sample every channel as fast as the interpreter allows."""
    histograms = [[0] * BUCKETS for _ in adcs]
    extremes = [[ADC_FULL_SCALE, 0] for _ in adcs]
    count = 0

    deadline = time.ticks_add(time.ticks_ms(), seconds * 1000)
    while time.ticks_diff(deadline, time.ticks_ms()) > 0:
        for index, adc in enumerate(adcs):
            raw = adc.read_u16()
            bucket = raw * BUCKETS // (ADC_FULL_SCALE + 1)
            histograms[index][bucket] += 1
            if raw < extremes[index][0]:
                extremes[index][0] = raw
            if raw > extremes[index][1]:
                extremes[index][1] = raw
        count += 1

    return histograms, extremes, count


def volts(raw):
    return raw * ADC_VREF / ADC_FULL_SCALE


def bucket_volts(bucket):
    return (bucket + 0.5) * ADC_VREF / BUCKETS


def report(name, histogram, extremes, total):
    low_raw, high_raw = extremes
    print("\n  channel {}".format(name))
    print("    min {:.3f} V     max {:.3f} V".format(
        volts(low_raw), volts(high_raw)))

    # Split at the midpoint and take the densest bucket on each side. A clean
    # digital signal puts nearly all its time at two levels; anything else
    # means the signal is not switching cleanly.
    midpoint = BUCKETS // 2
    low_side = histogram[:midpoint]
    high_side = histogram[midpoint:]

    low_count = sum(low_side)
    high_count = sum(high_side)
    if not low_count or not high_count:
        print("    only one level seen - was the wheel turning?")
        return None

    low_peak = low_side.index(max(low_side))
    high_peak = midpoint + high_side.index(max(high_side))

    v_low = bucket_volts(low_peak)
    v_high = bucket_volts(high_peak)
    duty = 100 * high_count / total

    print("    resting low  ~{:.2f} V   ({:.0f}% of samples)".format(
        v_low, 100 * low_count / total))
    print("    driven high  ~{:.2f} V   ({:.0f}% of samples)".format(
        v_high, duty))
    print("    implied encoder output: {:.2f} V".format(
        v_high / divider_ratio()))

    # The top bucket is open-ended: everything from its lower edge to the ADC's
    # ceiling lands in it, and anything above the ceiling lands in it too. So a
    # high level reported there is a lower bound, not a measurement, and every
    # figure derived from it - the fraction of supply, the implied pull-up - is
    # derived from a number the ADC could not actually see.
    #
    # This matters here specifically because the three wirings worth telling
    # apart (5 V through the divider, 3V3 direct, and 5 V direct, which is over
    # the part's absolute maximum) all land in this one bucket.
    saturated = high_peak >= BUCKETS - 1
    if saturated:
        print("    ^ TOP BUCKET: >= {:.2f} V, upper edge unknown."
              .format(bucket_volts(high_peak) - ADC_VREF / BUCKETS / 2))
        print("      The ADC cannot see above its own ceiling, so this is a")
        print("      floor rather than a reading.")

    return v_high, saturated, duty


def main():
    print("\nencoder output probe")
    print("=" * 46)
    print("  divider {:.0f}k / {:.0f}k  (ratio {:.3f})".format(
        R_TOP_K, R_BOTTOM_K, divider_ratio()))
    print("  reading {} (A) and {} (B)".format(
        *["GP{}".format(pin) for _, pin in CHANNELS]))
    print("\n  TURN THE HANDWHEEL STEADILY for the next {}s...".format(
        SAMPLE_SECONDS))

    adcs = [ADC(Pin(pin)) for _, pin in CHANNELS]

    if _ESP32:
        # Without this the ESP32 reads only about 0.95 V full scale, and every
        # logic high on a 3.3 V part pins at the top of the range. The failure
        # is silent and convincing: both clusters land at "full scale", the
        # tool reports a healthy separation between 0 V and 3.3 V, and the
        # number it printed for the high level was never measured at all.
        for adc in adcs:
            adc.atten(ADC.ATTN_11DB)
    histograms, extremes, count = sample(adcs, SAMPLE_SECONDS)

    print("\n  {} samples per channel".format(count))

    highs = []
    duties = []
    saturated = False
    for index, (name, _pin) in enumerate(CHANNELS):
        result = report(name, histograms[index], extremes[index], count)
        if result is not None:
            highs.append(result[0])
            saturated = saturated or result[1]
            duties.append((name, result[2]))

    if not highs:
        print("\n  no switching seen. check the wheel was turning, and that")
        print("  the encoder really is on {} and {}.".format(
            *["GP{}".format(pin) for _, pin in CHANNELS]))
        print("  On the 3V3 trial this is also what a wheel that will not run")
        print("  at 3.3 V looks like - measure its supply before its output.")
        return 1

    # In quadrature the two channels are the same square wave a quarter cycle
    # apart, so over many detents both must approach 50/50. A lopsided pair
    # means one channel is barely transitioning while the other is - which is
    # what a bad joint looks like, and is how one was actually found here
    # (channel B at 93/7 against A at 63/37, traced to a bad wire).
    #
    # Worth flagging rather than leaving to the eye, because the numbers were
    # printed above the whole time and still needed someone to notice them. On
    # this board it is one of only two signals available: PCNT reports no
    # errors of its own, so duty and the scale check carry everything.
    if len(duties) == 2:
        spread = abs(duties[0][1] - duties[1][1])
        if spread > 25:
            print("\n  DUTY MISMATCH: {} at {:.0f}% high, {} at {:.0f}%."
                  .format(duties[0][0], duties[0][1],
                          duties[1][0], duties[1][1]))
            print("  Both should approach 50% over many detents. This means")
            print("  one channel is barely switching - suspect its wire or")
            print("  joint before anything subtler, and re-run turning")
            print("  steadily through the whole window.")
        elif spread > 12:
            print("\n  duty {:.0f}% / {:.0f}% - uneven, but within what a short"
                  .format(duties[0][1], duties[1][1]))
            print("  or hesitant turn produces. Turn steadily to be sure.")

    junction_high = sum(highs) / len(highs)
    encoder_high = junction_high / divider_ratio()

    print("\n" + "=" * 46)
    # Said as plainly as possible, because it is a constant at the top of this
    # file and not a measurement. Nothing here can see the supply rail; the
    # word "assumed" was doing that work before and was too quiet about it.
    print("  SUPPLY_V says {:.2f} V. That is a setting in this file, NOT"
          .format(SUPPLY_V))
    print("  measured - if the encoder is on a different rail, every")
    print("  conclusion below is wrong. Check it before believing them.")
    print("  encoder drives its output to ~{:.2f} V".format(encoder_high))

    if saturated:
        print("\n  The high level hit the top of the ADC's range, so the")
        print("  figures below are computed from a floor. Three wirings all")
        print("  land there and they are not the same thing:")
        print("    5 V through the 22k divider  -> ~3.1-3.3 V, fine")
        print("    3V3 direct, resistors out    -> 3.3 V, fine")
        print("    5 V direct, resistors out    -> 5 V, over absolute max")
        print("  A meter on the encoder's Vcc separates them in one reading.")

    # Judged as a fraction of its own supply, so the same reasoning holds at
    # 5 V and at 3V3.
    fraction = encoder_high / SUPPLY_V if SUPPLY_V else 0.0
    if saturated:
        # No source-impedance conclusion is available from a clipped level, and
        # printing "implied internal pull-up: ~11.1k" off one reads as a
        # measurement of the encoder rather than an artefact of the ADC.
        print("\n  Source impedance not calculated - see above.")
    elif fraction > 0.92:
        print("  -> {:.0f}% of supply: full swing. Either a push-pull driver,"
              .format(fraction * 100))
        print("     or open-collector with nothing loading the pull-up, which")
        print("     is what removing the 22k is supposed to produce.")
    elif fraction > 0.80:
        print("  -> {:.0f}% of supply: nearly full, slight droop under load."
              .format(fraction * 100))
    else:
        print("  -> {:.0f}% of supply, so the output has real source"
              .format(fraction * 100))
        print("     impedance: open-collector with an internal pull-up rather")
        print("     than a line driver, loaded by whatever is to ground.")
        pullup_k = R_TOP_K + R_BOTTOM_K
        if pullup_k and encoder_high:
            implied = pullup_k * (SUPPLY_V - encoder_high) / encoder_high
            print("     implied internal pull-up: ~{:.1f}k".format(implied))

    # The verdict that actually decides the 3V3 question.
    print("\n  input-high threshold here is ~{:.2f} V".format(INPUT_HIGH_V))
    margin = junction_high - INPUT_HIGH_V
    if margin <= 0:
        print("  FAILS by {:.2f} V - the pin will not see a reliable high."
              .format(-margin))
        print("  If the 22k is still fitted, that is the reason: remove it.")
    elif margin < 0.3:
        print("  clears it by only {:.2f} V - works on the bench and is the"
              .format(margin))
        print("  kind of margin that stops working on a cold morning.")
    else:
        print("  clears it by {:.2f} V - comfortable.".format(margin))
    return 0


raise SystemExit(main())
