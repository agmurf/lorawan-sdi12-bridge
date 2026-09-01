#!/usr/bin/env python3
"""
SDI-12 health, measured from the slave's own log.

WHY LATENCY IS THE HEADLINE
SDI-12 gives a sensor 15 ms to begin its reply. Miss that and the
recorder treats the sensor as absent -- there is no retry, no error, and
nothing in the ERT-A2 to say what happened. It looks exactly like a dead
gauge. So the number that matters is not the average, it is the WORST
case and how close it runs to 15 ms.

WHY THE SENTINEL FRACTION IS THE OTHER HALF
A poll that is answered promptly with -9.999 is a healthy SDI-12 link
carrying bad news: the radio path had nothing fresh. Separating those two
failure modes is the whole point -- one is fixed at the gateway, the
other at the antenna.

Reads a journal on stdin.
"""
import re
import statistics as st
import sys

LINE = re.compile(
    r"(?P<time>\d{2}:\d{2}:\d{2}).*sdi12\] "
    r"'(?P<cmd>[^']+)' -> '(?P<resp>[^']*)'\s+(?P<ms>[\d.]+)ms"
)
DEADLINE_MS = 15.0          # SDI-12 v1.4: reply must begin within 15 ms
SENTINEL = "-9.999"


def hhmmss_to_s(t):
    h, m, s = (int(x) for x in t.split(":"))
    return h * 3600 + m * 60 + s


def main():
    ev = [m.groupdict() for m in LINE.finditer(sys.stdin.read())]
    if not ev:
        print("no SDI-12 activity in this window")
        return 0

    for e in ev:
        e["ms"] = float(e["ms"])

    print(f"SDI-12 events   : {len(ev)}  "
          f"({ev[0]['time']} to {ev[-1]['time']} UTC)")

    # --- timing, per command --------------------------------------------
    print()
    worst_overall = 0.0
    for cmd in sorted({e["cmd"] for e in ev}):
        sub = [e["ms"] for e in ev if e["cmd"] == cmd]
        worst = max(sub)
        worst_overall = max(worst_overall, worst)
        over = sum(1 for x in sub if x > DEADLINE_MS)
        near = sum(1 for x in sub if DEADLINE_MS * 0.66 < x <= DEADLINE_MS)
        print(f"{cmd:<8} n={len(sub):<4} "
              f"median {st.median(sub):5.1f} ms   worst {worst:5.1f} ms   "
              f"margin {DEADLINE_MS - worst:+5.1f} ms")
        if over:
            print(f"         *** {over} reply(s) BLEW the {DEADLINE_MS:.0f} ms "
                  f"deadline -- the recorder saw a dead sensor ***")
        elif near:
            print(f"         {near} reply(s) within a third of the deadline "
                  f"-- little headroom")

    print()
    if worst_overall > DEADLINE_MS:
        print(f"VERDICT: TIMING FAULT. Worst {worst_overall:.1f} ms exceeds "
              f"the {DEADLINE_MS:.0f} ms deadline.")
    elif worst_overall > DEADLINE_MS * 0.66:
        print(f"VERDICT: timing OK but tight. Worst {worst_overall:.1f} ms "
              f"against a {DEADLINE_MS:.0f} ms deadline.")
    else:
        print(f"VERDICT: timing healthy. Worst {worst_overall:.1f} ms, "
              f"{DEADLINE_MS - worst_overall:.1f} ms of margin.")

    # --- what the recorder actually received ----------------------------
    data = [e for e in ev if e["cmd"].endswith("D0!")]
    if data:
        bad = [e for e in data if SENTINEL in e["resp"]]
        print()
        print(f"data polls      : {len(data)}")
        print(f"  sentinel      : {len(bad)} "
              f"({100.0 * len(bad) / len(data):.0f}%) -- no fresh radio data")
        print(f"  real readings : {len(data) - len(bad)} "
              f"({100.0 * (len(data) - len(bad)) / len(data):.0f}%)")
        vals = []
        for e in data:
            m = re.search(r"0([+-][\d.]+)", e["resp"])
            if m and SENTINEL not in e["resp"]:
                vals.append(float(m.group(1)))
        if vals:
            print(f"  values served : {min(vals):.3f} to {max(vals):.3f} m")

    # --- poll cadence ----------------------------------------------------
    if len(data) > 1:
        ts = [hhmmss_to_s(e["time"]) for e in data]
        gaps = [b - a for a, b in zip(ts, ts[1:]) if b > a]
        if gaps:
            print()
            print(f"poll interval   : median {st.median(gaps) / 60:.1f} min "
                  f"(min {min(gaps) / 60:.1f}, max {max(gaps) / 60:.1f})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
