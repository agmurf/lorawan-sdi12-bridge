#!/usr/bin/env python3
"""
One command that answers "is the redundant flood path actually working?"

WHY THIS EXISTS
Every failure this project has had was SILENT. The gateway kept running,
the services stayed green, the logs looked healthy -- and the data quietly
stopped or halved:

  * stock channel plan restored  -> 25% reception, nothing logged
  * height band too narrow       -> every reading rejected as invalid
  * SDI-12 command corrupted     -> polls unanswered, looks like a dead gauge
  * ERT-A2 unplugged             -> no polls at all for 2h20m
  * volatile journal + reboot    -> days of measurement erased

"systemctl is-active" reports green for every one of those. So this checks
the things that actually carry data, not the things that are merely
running.

EXIT CODES
    0  OK    -- delivering normally
    1  WARN  -- degraded but delivering
    2  FAIL  -- the path is not delivering; act now

Safe to run from cron or a systemd timer.
"""
import json
import os
import re
import subprocess
import sys
import time

EXPECTED_CHANNELS = [922.0, 922.2, 922.4, 922.6, 922.8, 923.0, 923.2, 923.4]
CONF = "/opt/ttn-gateway/packet_forwarder/lora_pkt_fwd/global_conf.json"
STATE = "/home/pi/river_state.json"
SERVICES = ["ttn-gateway", "local-ns-logger", "sdi12-slave", "pigpiod"]
SDI12_DEADLINE_MS = 15.0
WINDOW = "-3h"

# Reception below this is not weather, it is a configuration regression.
RECEPTION_WARN = 70.0
RECEPTION_FAIL = 40.0
# The ERT-A2 polls every 5 min, so three missed polls means it has stopped.
SDI12_SILENT_FAIL_S = 20 * 60
STATE_STALE_WARN_S = 30 * 60

results = []


def check(name, status, detail):
    results.append((status, name, detail))


def sh(*args):
    try:
        return subprocess.run(args, capture_output=True, text=True,
                              timeout=60).stdout
    except Exception:
        return ""


def journal(unit, since=WINDOW):
    return sh("journalctl", "-u", unit, "--since", since, "--no-pager")


# --- services ----------------------------------------------------------
for svc in SERVICES:
    active = sh("systemctl", "is-active", svc).strip()
    enabled = sh("systemctl", "is-enabled", svc).strip()
    if active != "active":
        check(svc, "FAIL", "not running (%s)" % active)
    elif enabled != "enabled":
        # Running now, but a power cut would not bring it back.
        check(svc, "WARN", "running but NOT enabled at boot")
    else:
        check(svc, "OK", "active, enabled")

# --- the channel plan, read from the LIVE config -----------------------
try:
    sx = json.load(open(CONF))["SX1301_conf"]
    live = []
    for i in range(8):
        ch = sx["chan_multiSF_%d" % i]
        if ch.get("enable"):
            centre = sx["radio_%d" % ch["radio"]]["freq"]
            live.append(round((centre + ch["if"]) / 1e6, 1))
    if sorted(live) == EXPECTED_CHANNELS:
        check("channel plan", "OK", "922.0-923.4, 8 channels")
    else:
        check("channel plan", "FAIL",
              "WRONG: %s (expected 922.0-923.4). Reception will sit near "
              "25%%. Restart ttn-gateway to re-enforce." % sorted(live))
except Exception as exc:
    check("channel plan", "FAIL", "cannot read %s (%s)" % (CONF, exc))

# --- reception, measured from the frame counter ------------------------
log = journal("local-ns-logger")
frames = [(int(m.group(1)), m.group(2)) for m in re.finditer(
    r"EM411 \S+: .*?fcnt=(\d+) .*?(\d{3}\.\d)MHz", log)]
if len(frames) < 3:
    check("reception", "WARN",
          "only %d frames in %s, too few to judge" % (len(frames), WINDOW))
else:
    fc = sorted(f[0] for f in frames)
    sent = fc[-1] - fc[0] + 1
    pct = 100.0 * len(fc) / sent
    chans = sorted({f[1] for f in frames})
    detail = "%d of %d (%.0f%%) across %d channel(s)" % (
        len(fc), sent, pct, len(chans))
    if pct < RECEPTION_FAIL:
        check("reception", "FAIL", detail + " -- check the channel plan first")
    elif pct < RECEPTION_WARN:
        check("reception", "WARN", detail)
    else:
        check("reception", "OK", detail)

# --- SDI-12: is the recorder still asking, and are we answering? -------
sd = journal("sdi12-slave")
polls = re.findall(
    r"(\d{2}:\d{2}:\d{2}).*sdi12\] '[^']*D0!' -> '([^']*)'\s+([\d.]+)ms", sd)
if not polls:
    check("SDI-12 polls", "FAIL",
          "ERT-A2 has not polled in %s -- check its power and USB lead"
          % WINDOW)
else:
    hh, mm, ss = (int(x) for x in polls[-1][0].split(":"))
    now = time.gmtime()
    age = (now.tm_hour * 3600 + now.tm_min * 60 + now.tm_sec) - (
        hh * 3600 + mm * 60 + ss)
    if age < 0:
        age += 86400
    if age > SDI12_SILENT_FAIL_S:
        check("SDI-12 polls", "FAIL",
              "last poll %d min ago -- the recorder has stopped asking"
              % (age // 60))
    else:
        check("SDI-12 polls", "OK",
              "%d polls, last %d min ago" % (len(polls), age // 60))

    worst = max(float(p[2]) for p in polls)
    if worst > SDI12_DEADLINE_MS:
        check("SDI-12 timing", "FAIL",
              "worst %.1f ms exceeds the %.0f ms deadline -- the recorder "
              "sees a dead sensor" % (worst, SDI12_DEADLINE_MS))
    else:
        check("SDI-12 timing", "OK", "worst %.1f ms, %.1f ms margin"
              % (worst, SDI12_DEADLINE_MS - worst))

    bad = sum(1 for p in polls if "-9.999" in p[1])
    frac = 100.0 * bad / len(polls)
    detail = "%d of %d polls (%.0f%%) served the sentinel" % (
        bad, len(polls), frac)
    if frac > 50:
        check("SDI-12 data", "FAIL", detail)
    elif frac > 20:
        check("SDI-12 data", "WARN", detail)
    else:
        check("SDI-12 data", "OK", detail)

# --- is the published reading fresh? -----------------------------------
try:
    st = json.load(open(STATE))
    age = time.time() - st["updated"]
    val = st["values"][0]
    if age > STATE_STALE_WARN_S:
        check("river_state", "WARN", "%.0f min old (%.3f m)" % (age / 60, val))
    else:
        check("river_state", "OK", "%.3f m, %.0f min old" % (val, age / 60))
except Exception as exc:
    check("river_state", "FAIL", "unreadable (%s)" % exc)

# --- things that quietly erode -----------------------------------------
try:
    if "Storage=persistent" in open("/etc/systemd/journald.conf").read():
        check("journal", "OK", "persistent")
    else:
        check("journal", "WARN", "volatile -- history lost on every reboot")
except Exception:
    pass

if sh("timedatectl", "show", "-p", "NTPSynchronized",
      "--value").strip() == "yes":
    check("clock", "OK", "NTP synced")
else:
    check("clock", "WARN", "NOT synced -- log timestamps unreliable")

try:
    stv = os.statvfs("/")
    used = 100.0 * (1 - float(stv.f_bavail) / stv.f_blocks)
    check("disk", "OK" if used < 90 else "WARN", "%.0f%% used" % used)
except Exception:
    pass

# --- the decoder's crypto dependency is NOT system-wide ---------------
# pycryptodome lives in /home/pi/.local, so local-ns-logger only imports it
# because the unit runs as User=pi. Change that user, or wipe ~/.local, and
# the LoRa decode path dies at start with an ImportError.
dep = sh("sudo", "-u", "pi", "python3", "-c",
         "import Crypto, serial; print('ok')").strip()
if dep == "ok":
    check("dependencies", "OK", "Crypto + serial importable as pi")
else:
    check("dependencies", "FAIL",
          "pi cannot import Crypto/serial -- local-ns-logger cannot decode. "
          "They live in /home/pi/.local, not system-wide.")

# --- report -------------------------------------------------------------
order = {"FAIL": 0, "WARN": 1, "OK": 2}
worst = min((order[r[0]] for r in results), default=2)
print("=" * 66)
print("  HydroLoRaPi health  --  %s"
      % time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()))
print("=" * 66)
for status, name, detail in sorted(results, key=lambda r: order[r[0]]):
    print("  %-5s %-16s %s" % (status, name, detail))
print("-" * 66)
print("  VERDICT: %s" % {0: "FAIL -- the path is not delivering",
                         1: "WARN -- degraded but delivering",
                         2: "OK -- delivering normally"}[worst])
print("=" * 66)
sys.exit({0: 2, 1: 1, 2: 0}[worst])
