#!/usr/bin/env python3
"""
Enforce this gateway's LoRa channel plan before the packet forwarder starts.

THE PROBLEM THIS SOLVES
The RAK image ships an AS923 plan covering 923.2 - 924.6 MHz. The EM411 at
this site is AS923 as well, but its eight channels run 922.0 - 923.4: the
two AS923-1 defaults (923.2, 923.4) plus six added DOWNWARD. RAK's preset
adds its six UPWARD. The two plans therefore overlap on exactly the two
default channels, so the gateway heard 2 uplinks in every 8.

    stock plan  923.2 - 924.6     25-29% reception  (measured, weeks)
    this plan   922.0 - 923.4     93%+              (measured)

For a long time that looked like an antenna or path problem. It was not.
Every symptom -- losses that were all-or-nothing, no marginal frames, and
loss rates uncorrelated with signal strength -- follows from listening on
the wrong six channels.

WHY THIS RUNS AT EVERY START, NOT AS A ONE-OFF EDIT
global_conf.json lives under /opt/ttn-gateway and belongs to the RAK
installer. A package update, a reinstall or an SD reflash restores the
stock plan, and the failure is SILENT: the service still starts, the logs
still look healthy, and reception simply drops back to a quarter. Nothing
would point at the cause. Re-applying on every start removes that whole
class of regression.

Idempotent by design: it writes only when the plan is actually wrong, so a
normal boot costs one read and no SD-card writes.

FAILURE POLICY
If the config cannot be read or written this logs loudly and exits 0. A
gateway running the wrong plan still delivers a quarter of the data; one
that refuses to start delivers none. On a flood-warning path, degraded
beats dead -- but the journal will say so plainly.
"""
import collections
import json
import os
import sys

CONF = "/opt/ttn-gateway/packet_forwarder/lora_pkt_fwd/global_conf.json"

# Measured channel set of the deployed EM411-RDL (DevEUI and DevAddr withheld),
# confirmed by decoding on every one of these and on none outside them.
RADIO_0 = 922400000          # serves 922.0 .. 922.8
RADIO_1 = 923200000          # serves 923.0 .. 923.4
PLAN = [
    (0, -400000),   # 922.0
    (0, -200000),   # 922.2
    (0,       0),   # 922.4
    (0,  200000),   # 922.6
    (0,  400000),   # 922.8
    (1, -200000),   # 923.0
    (1,       0),   # 923.2   AS923-1 default
    (1,  200000),   # 923.4   AS923-1 default
]


def log(msg):
    print("[channel-plan] %s" % msg, flush=True)


def main():
    if not os.path.exists(CONF):
        log("ERROR: %s not found -- cannot enforce plan, "
            "gateway will run with whatever it has" % CONF)
        return 0

    try:
        with open(CONF) as fh:
            conf = json.load(fh, object_pairs_hook=collections.OrderedDict)
        sx = conf["SX1301_conf"]
    except Exception as exc:
        log("ERROR: cannot parse %s (%s) -- leaving it alone" % (CONF, exc))
        return 0

    changed = []

    if sx.get("radio_0", {}).get("freq") != RADIO_0:
        changed.append("radio_0 %s -> %d"
                       % (sx.get("radio_0", {}).get("freq"), RADIO_0))
        sx["radio_0"]["freq"] = RADIO_0
    if sx.get("radio_1", {}).get("freq") != RADIO_1:
        changed.append("radio_1 %s -> %d"
                       % (sx.get("radio_1", {}).get("freq"), RADIO_1))
        sx["radio_1"]["freq"] = RADIO_1
    for i in (0, 1):
        if not sx.get("radio_%d" % i, {}).get("enable"):
            changed.append("radio_%d enabled" % i)
            sx["radio_%d" % i]["enable"] = True

    for idx, (radio, iff) in enumerate(PLAN):
        key = "chan_multiSF_%d" % idx
        ch = sx.setdefault(key, collections.OrderedDict())
        if (ch.get("enable") is not True or ch.get("radio") != radio
                or ch.get("if") != iff):
            changed.append("%s -> radio %d IF %+d" % (key, radio, iff))
            ch["enable"] = True
            ch["radio"] = radio
            ch["if"] = iff

    if not changed:
        log("plan already correct (922.0 - 923.4, 8 channels) -- no change")
        return 0

    try:
        tmp = CONF + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(conf, fh, indent=4)
        os.replace(tmp, CONF)
    except Exception as exc:
        log("ERROR: cannot write %s (%s) -- gateway may run the stock plan "
            "and receive ~25%% of uplinks" % (CONF, exc))
        return 0

    log("STOCK PLAN DETECTED AND CORRECTED -- %d item(s):" % len(changed))
    for c in changed:
        log("    " + c)
    for idx, (radio, iff) in enumerate(PLAN):
        centre = RADIO_0 if radio == 0 else RADIO_1
        log("    chan %d  radio %d  %7.1f MHz"
            % (idx, radio, (centre + iff) / 1e6))
    return 0


if __name__ == "__main__":
    sys.exit(main())
