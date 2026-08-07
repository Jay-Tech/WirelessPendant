"""Round-trip and framing tests for the pendant protocol. Runs on a PC.

    python tools/test_protocol.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "micropython"))
from pendant import protocol as p  # noqa: E402

failures = []


def check(label, got, want):
    ok = got == want
    print("  {:<48} {}".format(label, "PASS" if ok else "FAIL"))
    if not ok:
        print("      got  {!r}\n      want {!r}".format(got, want))
        failures.append(label)


def decode_all(decoder, chunk):
    return list(decoder.feed(chunk))


print("protocol round trip")

d = p.LineDecoder()
check("hello survives encode/decode",
      decode_all(d, p.encode(p.hello())),
      [{"t": "hello", "dev": p.DEVICE, "ver": p.VERSION}])

d = p.LineDecoder()
check("jog carries signed detents and step",
      decode_all(d, p.encode(p.jog("X", -3, 0.1))),
      [{"t": "jog", "axis": "X", "det": -3, "step": 0.1}])

d = p.LineDecoder()
check("button down flag is a real bool",
      decode_all(d, p.encode(p.button("feed_hold", 1))),
      [{"t": "btn", "id": "feed_hold", "down": True}])

d = p.LineDecoder()
check("status round trips position and overrides",
      decode_all(d, p.encode(p.status("Run", (1.5, -2.0, 3.25), 110, 90))),
      [{"t": "status", "state": "Run", "wpos": [1.5, -2.0, 3.25],
        "fro": 110, "sro": 90}])

print("\nframing")

# Several messages in one TCP read.
d = p.LineDecoder()
batch = p.encode(p.ping(1)) + p.encode(p.ping(2)) + p.encode(p.ping(3))
check("three messages in one chunk",
      [m["seq"] for m in d.feed(batch)], [1, 2, 3])

# One message split across reads, byte by byte - the worst case TCP can hand us.
d = p.LineDecoder()
payload = p.encode(p.jog("Z", 7, 0.01))
received = []
for i in range(len(payload)):
    received.extend(d.feed(payload[i:i + 1]))
check("message split into single bytes reassembles",
      received, [{"t": "jog", "axis": "Z", "det": 7, "step": 0.01}])

# A chunk boundary landing mid-object, with the tail arriving later.
d = p.LineDecoder()
two = p.encode(p.ping(9)) + p.encode(p.ping(10))
split = len(two) - 6
first = list(d.feed(two[:split]))
second = list(d.feed(two[split:]))
check("partial trailing message waits for its newline",
      ([m["seq"] for m in first], [m["seq"] for m in second]), ([9], [10]))

print("\nrobustness")

d = p.LineDecoder()
check("blank lines are skipped, not counted as errors",
      (decode_all(d, b"\n\n" + p.encode(p.ping(1))), d.malformed),
      ([{"t": "ping", "seq": 1}], 0))

d = p.LineDecoder()
out = decode_all(d, b"{not json}\n" + p.encode(p.ping(2)))
check("malformed line is dropped, stream recovers",
      (out, d.malformed), ([{"t": "ping", "seq": 2}], 1))

d = p.LineDecoder()
out = decode_all(d, b'{"no_type":1}\n' + p.encode(p.ping(3)))
check("object with no type field is rejected",
      (out, d.malformed), ([{"t": "ping", "seq": 3}], 1))

d = p.LineDecoder()
out = decode_all(d, b'["a","list"]\n' + p.encode(p.ping(4)))
check("non-object JSON is rejected",
      (out, d.malformed), ([{"t": "ping", "seq": 4}], 1))

# A peer that never sends a newline must not grow the buffer without bound.
d = p.LineDecoder()
d.feed(b"x" * (p.LineDecoder.MAX_LINE + 100))
check("runaway line is dropped rather than buffered",
      (d.overflows, len(d._buffer)), (1, 0))
check("decoder still works after an overflow",
      decode_all(d, p.encode(p.ping(5))), [{"t": "ping", "seq": 5}])

print()
if failures:
    print("{} FAILED: {}".format(len(failures), ", ".join(failures)))
    sys.exit(1)
print("all protocol tests passed")
