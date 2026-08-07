"""Measure what the encoder actually puts out, using the ADC as a scope.

A multimeter cannot answer this. One detent of a 100 PPR wheel is one full
quadrature cycle, so both channels return to their resting state at every
click - catching the high level means holding the wheel between detents, and a
DVM on a moving signal reads the average rather than either level. Sampling
fast and reporting the two clusters gives the real answer.

Wire the DIVIDER OUTPUTS (the junctions, not the encoder pins) to ADC inputs:

    channel A junction  ->  GP26
    channel B junction  ->  GP27

This is safe by construction: a 10k/20k divider on a 4.9 V supply cannot
exceed 3.27 V, well inside the ADC's range. Do not connect the encoder pins
directly - that is the thing we are trying to find out about.

Run it, then turn the handwheel steadily for the whole sampling window:

    python -m mpremote connect id:7BE7DD09548134C0 run micropython/pendant/probe_encoder.py
"""

import time
from machine import ADC, Pin

SAMPLE_SECONDS = 6
ADC_FULL_SCALE = 65535
ADC_VREF = 3.3

# The divider that is physically installed, so the tool can report the encoder
# side of it rather than just its own input.
R_TOP_K = 10.0
R_BOTTOM_K = 20.0

CHANNELS = (("A", 26), ("B", 27))

# Levels are sorted into buckets and the two densest clusters reported. 40
# buckets over 3.3 V is ~82 mV of resolution, fine enough to separate a logic
# low from a logic high and coarse enough that noise does not fragment them.
BUCKETS = 40


def divider_ratio():
    return R_BOTTOM_K / (R_TOP_K + R_BOTTOM_K)


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
    return v_high


def main():
    print("\nencoder output probe")
    print("=" * 46)
    print("  divider {:.0f}k / {:.0f}k  (ratio {:.3f})".format(
        R_TOP_K, R_BOTTOM_K, divider_ratio()))
    print("  reading junctions on GP26 (A) and GP27 (B)")
    print("\n  TURN THE HANDWHEEL STEADILY for the next {}s...".format(
        SAMPLE_SECONDS))

    adcs = [ADC(Pin(pin)) for _, pin in CHANNELS]
    histograms, extremes, count = sample(adcs, SAMPLE_SECONDS)

    print("\n  {} samples per channel".format(count))

    highs = []
    for index, (name, _pin) in enumerate(CHANNELS):
        result = report(name, histograms[index], extremes[index], count)
        if result is not None:
            highs.append(result)

    if not highs:
        print("\n  no switching seen. check the wheel was turning, and that")
        print("  the junctions really are wired to GP26/GP27.")
        return 1

    junction_high = sum(highs) / len(highs)
    encoder_high = junction_high / divider_ratio()

    print("\n" + "=" * 46)
    print("  encoder drives its output to ~{:.2f} V".format(encoder_high))
    if encoder_high > 4.5:
        print("  -> full-swing push-pull. The divider is correct; if the")
        print("     junction reads low, suspect the divider wiring.")
    elif encoder_high > 3.9:
        print("  -> nearly full swing, slight droop under load. Divider fine.")
    else:
        print("  -> well below its 4.9 V supply, so the output has real")
        print("     source impedance: open-collector with an internal")
        print("     pull-up rather than a line driver. The divider is the")
        print("     wrong topology - the internal pull-up should form the")
        print("     top leg, with a single resistor to ground.")
        pullup_k = R_TOP_K + R_BOTTOM_K
        implied = pullup_k * (4.909 - encoder_high) / encoder_high
        print("     implied internal pull-up: ~{:.1f}k".format(implied))
    print("  input-high threshold is ~2.15 V; aim for 3.0-3.3 V.")
    return 0


raise SystemExit(main())
