"""Tests for button debouncing and long-press. Runs on a PC.

    python tools/test_buttons.py
"""

import sys
import types
from pathlib import Path


class _FakePin:
    IN = 0
    PULL_UP = 1

    def __init__(self, ident, mode=None, pull=None):
        self.ident = ident
        self._value = 1  # idle high, matching an active-low button

    def value(self, val=None):
        if val is None:
            return self._value
        self._value = val


_machine = types.ModuleType("machine")
_machine.Pin = _FakePin
sys.modules["machine"] = _machine

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "micropython"))
from pendant.buttons import (  # noqa: E402
    Button, ButtonPanel, Debouncer, DEBOUNCE_MS, LONG_PRESS_MS,
    PRESS, RELEASE, LONG_PRESS)

failures = []


def check(label, got, want):
    ok = got == want
    print("  {:<52} {}".format(label, "PASS" if ok else "FAIL"))
    if not ok:
        print("      got  {!r}\n      want {!r}".format(got, want))
        failures.append(label)


print("debounce")

d = Debouncer(debounce_ms=20)
check("clean press settles after the debounce window",
      [d.update(True, t) for t in (0, 10, 20)], [None, None, True])

d = Debouncer(debounce_ms=20)
check("press before the window has elapsed is not reported",
      [d.update(True, t) for t in (0, 5, 19)], [None, None, None])

# The reason debouncing exists: a switch chatters on contact.
d = Debouncer(debounce_ms=20)
chatter = [(True, 0), (False, 2), (True, 4), (False, 6), (True, 8),
           (True, 18), (True, 28)]
events = [d.update(level, t) for level, t in chatter]
check("bouncing contact yields exactly one press",
      [e for e in events if e is not None], [True])

d = Debouncer(debounce_ms=20)
for t in (0, 25):
    d.update(True, t)
check("release settles the same way",
      [d.update(False, t) for t in (30, 40, 55)], [None, None, False])

d = Debouncer(debounce_ms=20)
for t in (0, 25):
    d.update(True, t)
check("holding produces no repeat events",
      [d.update(True, t) for t in (30, 100, 5000)], [None, None, None])

# A glitch shorter than the window must not register at all.
d = Debouncer(debounce_ms=20)
glitch = [d.update(True, 0), d.update(True, 5), d.update(False, 8),
          d.update(False, 30)]
check("spike shorter than the window is rejected",
      [e for e in glitch if e is not None], [])

print("\nbutton events")


def press_sequence(button, steps):
    """steps: (raw, now_ms) pairs. Returns the events produced."""
    return [button.poll(now, raw=raw) for raw, now in steps]


b = Button(4, "feed_hold")
events = press_sequence(b, [(True, 0), (True, 25), (False, 100), (False, 130)])
check("press then release",
      [e for e in events if e], [PRESS, RELEASE])

b = Button(4, "zero")
steps = [(True, 0), (True, 25), (True, 500),
         (True, 25 + LONG_PRESS_MS), (True, 2000), (False, 2100),
         (False, 2130)]
events = [e for e in press_sequence(b, steps) if e]
check("long press fires once while held, before release",
      events, [PRESS, LONG_PRESS, RELEASE])

b = Button(4, "zero")
steps = [(True, 0), (True, 25), (False, 200), (False, 230)]
events = [e for e in press_sequence(b, steps) if e]
check("short press does not produce a long press",
      events, [PRESS, RELEASE])

b = Button(4, "zero")
steps = [(True, 0), (True, 25)] + [(True, 25 + LONG_PRESS_MS + n * 50)
                                   for n in range(6)]
events = [e for e in press_sequence(b, steps) if e]
check("long press does not repeat while still held",
      events, [PRESS, LONG_PRESS])

print("\npanel")

panel = ButtonPanel([(4, "axis_next"), (5, "step_up"), (6, "feed_hold")])
check("idle panel reports nothing", panel.poll(0), [])

# Drive two buttons at once through their pins.
panel = ButtonPanel([(4, "axis_next"), (5, "step_up")])
panel.buttons[0]._pin.value(0)
panel.buttons[1]._pin.value(0)
panel.poll(0)
check("simultaneous presses are both reported",
      sorted(panel.poll(30)), [("axis_next", PRESS), ("step_up", PRESS)])

# An unwired pin idles high through its pull-up and must stay silent, so a
# partially populated panel is usable without editing the map.
panel = ButtonPanel([(4, "axis_next"), (5, "unwired")])
panel.buttons[0]._pin.value(0)
panel.poll(0)
check("unwired button stays silent while another is pressed",
      panel.poll(30), [("axis_next", PRESS)])

print()
if failures:
    print("{} FAILED: {}".format(len(failures), ", ".join(failures)))
    sys.exit(1)
print("all button tests passed")
