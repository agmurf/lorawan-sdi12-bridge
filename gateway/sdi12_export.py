#!/usr/bin/env python3
"""
Export what the ERT-A2 was actually SERVED on SDI-12, as CSV.

    python3 /home/pi/sdi12_export.py --since '-10h' > sdi12_served.csv

This is the ground truth half of the soak test: for every value the radio
transmitted, what was the gauge reading at that moment. Without it a decode
can only be eyeballed, which is how this project lost weeks.

TIMEZONE, AND WHY IT GETS AN OPTION RATHER THAN A CONSTANT
The gateway runs in UTC. The soak logger on ai1 stamps naive LOCAL time. A
join across the two is therefore off by the local offset -- ten hours here --
and nothing about the failure looks like a failure: correlate.py would match
each transmission against a poll from ten hours earlier and solve a perfectly
tidy mapping through the wrong points. So the conversion is explicit, named
in the output header comment, and defaults to the zone the decoder runs in
rather than to the zone this machine happens to be set to.

The -9.999 staleness sentinel is emitted as-is. It is not a level and must
not be averaged into one, but it is evidence: it says the gateway knew it had
nothing fresh, which is a different thing from the radio holding a stale value
on its own initiative.
"""
import argparse
import csv
import re
import subprocess
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

# 2026-09-01T07:25:11+0000 rak-gateway sdi12[462]: [sdi12] '0D0!' -> '0+0.397\r\n'  0.2ms
LINE = re.compile(
    r"^(?P<ts>\S+)\s+\S+\s+sdi12\[\d+\]:\s+\[sdi12\]\s+"
    r"'(?P<cmd>[^']*)'\s+->\s+'(?P<resp>[^']*)'")


def parse_response(resp):
    """'0+0.397\\r\\n' -> 0.397. Returns None if it is not a D0 data reply."""
    body = resp.replace("\\r", "").replace("\\n", "").strip()
    if len(body) < 2:
        return None
    try:
        return float(body[1:])        # drop the SDI-12 address character
    except ValueError:
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="-10h",
                    help="journalctl --since expression")
    ap.add_argument("--tz", default="Australia/Sydney",
                    help="emit timestamps in this zone, to match the naive "
                         "local stamps in soak_log.csv")
    ap.add_argument("--unit", default="sdi12-slave")
    args = ap.parse_args()

    try:
        zone = ZoneInfo(args.tz)
    except Exception as e:
        sys.exit(f"unknown timezone {args.tz!r}: {e}")

    out = subprocess.run(
        ["journalctl", "-u", args.unit, "--since", args.since,
         "-o", "short-iso", "--no-pager"],
        capture_output=True, text=True).stdout

    w = csv.writer(sys.stdout)
    w.writerow(["timestamp", "value"])
    n = sentinel = 0
    for line in out.splitlines():
        m = LINE.match(line)
        if not m or not m.group("cmd").endswith("D0!"):
            continue
        val = parse_response(m.group("resp"))
        if val is None:
            continue
        try:
            t = datetime.strptime(m.group("ts"), "%Y-%m-%dT%H:%M:%S%z")
        except ValueError:
            continue
        w.writerow([t.astimezone(zone).strftime("%Y-%m-%d %H:%M:%S"), f"{val:.3f}"])
        n += 1
        if val < -9:
            sentinel += 1

    sys.stderr.write(f"{n} polls exported in {args.tz} "
                     f"({sentinel} sentinel, {n - sentinel} real)\n")


if __name__ == "__main__":
    main()
