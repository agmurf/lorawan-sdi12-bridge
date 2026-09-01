#!/usr/bin/env python3
"""
Reception statistics for the EM411 link, computed from the frame counter.

WHY FCnt AND NOT WALL-CLOCK
Counting "frames per hour" cannot tell a missed reception from a sensor
that skipped a transmission -- and those need completely different
fixes. FCnt settles it: the device increments once per uplink whether or
not anybody hears it, so the span between the first and last frame we
received IS the number it sent. Everything missing from that span is our
loss, at our end, with no inference required.

It also reports the counters, and RECOVERED above all. That is the number
that tests whether rejecting frames on the concentrator's 16-bit CRC was
throwing away data the 32-bit MIC would have vouched for. If it stays at
zero, the CRC failures are genuine noise. If it climbs, they were not,
and every one of those frames was a reading we used to discard.

Reads a journal on stdin.
"""
import re
import statistics as st
import sys

FRAME = re.compile(
    r"(?P<time>\d{2}:\d{2}:\d{2}) UTC\] EM411 \S+: "
    r".*?fcnt=(?P<fcnt>\d+) adr=(?P<adr>\w+) "
    r"\[rx (?P<rssi>-?\d+)dBm (?P<snr>-?[\d.]+)dB (?P<sf>SF\d+)BW125"
)
HEARTBEAT = re.compile(
    r"accept=(\d+) drop=(\d+) published=(\d+) failed=(\d+) "
    r"crc_fail=(\d+)(?: recovered=(\d+))?"
)
NOTABLE = re.compile(r"(RECOVERED|MIC FAIL|FPORT REJECT) ")


def main():
    text = sys.stdin.read()

    frames = [{
        "time": m.group("time"), "fcnt": int(m.group("fcnt")),
        "adr": m.group("adr"), "rssi": int(m.group("rssi")),
        "snr": float(m.group("snr")), "sf": m.group("sf"),
    } for m in FRAME.finditer(text)]

    tot = dict(accepted=0, dropped=0, published=0, failed=0,
               crc_fail=0, recovered=0)
    for m in HEARTBEAT.finditer(text):
        tot["accepted"] += int(m.group(1))
        tot["dropped"] += int(m.group(2))
        tot["published"] += int(m.group(3))
        tot["failed"] += int(m.group(4))
        tot["crc_fail"] += int(m.group(5))
        tot["recovered"] += int(m.group(6) or 0)

    notable = [ln.strip() for ln in text.splitlines() if NOTABLE.search(ln)]

    # RECOVERED leads, because it is the one result that would change the
    # conclusion about where the losses are.
    print("=" * 62)
    if tot["recovered"]:
        print(f"*** RECOVERED = {tot['recovered']} ***")
        print("Frames the concentrator CRC condemned and the LoRaWAN MIC")
        print("vouched for. These are authentic readings that the previous")
        print("code discarded. The CRC was the wrong check to trust.")
    else:
        print("RECOVERED = 0")
        print("Every CRC-failed frame also failed the MIC, so they are")
        print("genuine noise-triggered detections, not our sensor's frames")
        print("being wrongly flagged.")
    print("=" * 62)

    print(f"counters      : accepted={tot['accepted']} "
          f"dropped={tot['dropped']} published={tot['published']} "
          f"crc_fail={tot['crc_fail']} recovered={tot['recovered']}")
    if tot["crc_fail"] + tot["accepted"] + tot["dropped"]:
        denom = tot["crc_fail"] + tot["accepted"] + tot["dropped"]
        print(f"crc-fail share: {100.0 * tot['crc_fail'] / denom:.0f}% "
              f"of frames the concentrator handed us")

    if not frames:
        print("\nno frames decoded in this window")
        return 0

    frames.sort(key=lambda f: f["fcnt"])
    first, last = frames[0]["fcnt"], frames[-1]["fcnt"]
    sent, got = last - first + 1, len(frames)

    print()
    print(f"window        : fcnt {first} -> {last}  "
          f"({frames[0]['time']} to {frames[-1]['time']} UTC)")
    print(f"sent by device: {sent}")
    print(f"received here : {got}")
    print(f"RECEPTION     : {100.0 * got / sent:.0f}%")
    print(f"ADR bit       : {', '.join(sorted({f['adr'] for f in frames}))}")

    print()
    for sf in sorted({f["sf"] for f in frames}, key=lambda s: int(s[2:])):
        sub = [f for f in frames if f["sf"] == sf]
        span = sub[-1]["fcnt"] - sub[0]["fcnt"] + 1
        rs = [f["rssi"] for f in sub]
        ss = [f["snr"] for f in sub]
        print(f"{sf}: {len(sub)} of {span} in its own fcnt span "
              f"({100.0 * len(sub) / span:.0f}%)")
        print(f"      rssi median {st.median(rs):.0f} dBm "
              f"(range {min(rs)} to {max(rs)}, spread {max(rs) - min(rs)} dB)")
        print(f"      snr  median {st.median(ss):.1f} dB "
              f"(range {min(ss)} to {max(ss)})")

    # A long run of consecutive misses matters more than the same number
    # scattered, because the SDI-12 staleness window is what the recorder
    # actually sees.
    have = {f["fcnt"] for f in frames}
    missing = [n for n in range(first, last + 1) if n not in have]
    if missing:
        runs, run = [], [missing[0]]
        for n in missing[1:]:
            if n == run[-1] + 1:
                run.append(n)
            else:
                runs.append(run)
                run = [n]
        runs.append(run)
        longest = max(len(r) for r in runs)
        print()
        print(f"missed        : {len(missing)} frames in {len(runs)} gap(s)")
        print(f"longest gap   : {longest} consecutive ({longest * 5} min)")
        print(f"staleness     : window is 20 min, so a gap of 4+ publishes "
              f"the -9.999 sentinel")

    if notable:
        print()
        print("notable events (recoveries / silent-path rejections):")
        for ln in notable[-15:]:
            print(f"  {ln[-150:]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
