# Three-protocol soak test — runbook

Verify the ERT-A2 rig transmits correctly under **iFLOWS**, **Enhanced
iFLOWS** and **ALERT2**, across protocol changes and sensor-ID changes, by
comparing what went out over VHF against an independent record of what the
rig was given.

    EM411 on SDI-12   the real product path, exact ground truth from the gateway
    battery internal  a second ID that moves slowly

    relay on DI1      NOT IN THIS RUN - faulty, to be fixed later

Losing the relay costs the self-validating leg: a counter that must only ever
increase can be checked with no reference file at all, and nothing else in the
rig can. Ground truth now rests entirely on joining the river channel against
the SDI-12 record. That works because the level is tidal and moves through
many distinct values in a day, which is what the mapping solve wants anyway -
but it means a decode is checkable only where a matching poll exists, and the
counter section of correlate.py will report nothing.

---

## Before you leave — one thing

Only the ERT-A2 login needs a person. The gateway and the soak are both
driven remotely.

**1. Power up `rak-gateway`.** It has been offline since 29 Aug. Without it
there is no EM411 leg and, more importantly, no ground truth for the river
channel. Confirm with:

    python3 /home/pi/healthcheck.py

**2. Log in to the ERT-A2 console** on `flooderrts`, then release the port:

    ssh pi@192.168.1.33
    picocom -b 115200 /dev/ttyACM0      # log in as Engineer
                                        # Ctrl-A Ctrl-X to exit, leaving the
                                        # session authenticated

`ert-keepalive.timer` then holds the session open with a bare CR every five
minutes. It only ever sends at the Main Menu, and stands off entirely while a
reconfiguration is in progress, so it cannot fire into the middle of a wizard.
**Leave the console at the Main Menu**, not inside a submenu.

**3. Nothing - the soak starts remotely.** The noBGP agent on `ai1` runs as
`AI_1\adamm`, not as SYSTEM, and Python313 carries numpy/scipy in its own
`Lib\site-packages`, so this needs nobody present:

    C:\Users\adamm\AppData\Local\Programs\Python\Python313\python.exe protocol_soak.py --hours 10

Name that interpreter explicitly. Bare `python` on this box resolves to 3.12,
whose numpy sits in the roaming per-user tree and is not reliably on the path.

---

## What runs unattended

| | |
|---|---|
| `protocol_soak.py` | decodes every window under all three protocols, logs to `soak_log.csv` |
| `ert-keepalive.timer` | holds the ERT-A2 session, every 5 min, guarded |
| `hydro-health.timer` | gateway verdict to the journal, every 30 min |
| `rxstats-log.timer` | reception + SDI-12 stats snapshot, every 15 min |

---

## The test matrix

Roughly 45 minutes per block, driven over the console from `flooderrts`.

1. **Baseline** — current protocol, current IDs. Confirm all three channels
   decode and match ground truth.
2. **Protocol A → B** — change protocol only, IDs unchanged. Same three
   channels must reappear under the new protocol.
3. **Protocol B → C** — again.
4. **ID change** — move off `4078`/`4079`. Those differ by a single bit, so
   one bit-flip in a river frame decodes as the DI channel. Pick IDs with
   greater Hamming distance and confirm the new IDs appear.
5. **Return to baseline** — confirm it comes back cleanly.

The soak decodes under all three protocols continuously, so nothing needs
switching on the receiving side at any point.

---

## Reading the result

    cd "C:\SDR ALERT Decoder"
    python correlate.py --soak soak_log.csv --sdi12 sdi12_served.csv

with the ground truth exported from the gateway:

    python3 /home/pi/sdi12_export.py --since '-9h' > sdi12_served.csv

What it reports:

* **per protocol and ID** — counts, value ranges, CRC pass rate, bit-12 count
* **cross-talk** — the same burst claimed by more than one protocol. Not
  necessarily an error, but a value trusted from the wrong one invents a
  station, so it is surfaced rather than silently resolved
* **river mapping** — solves `raw = slope x metres + offset` from matched
  pairs. **SOLVED, 1 Sep 2026: `raw = metres x 1000`, offset zero, residuals
  0.00 mm.** Two independent pairs, both exact:

      17:49:56  sent 474   <-  served 0.474 m at 17:45:11
      17:54:57  sent 500   <-  served 0.500 m at 17:50:11

  Match ages 285 s and 286 s, inside the 5-minute poll interval, so these
  are genuine adjacent pairs and not a timezone artefact. Competing
  candidates are not close: Level x100 is out by a mean 438, Raw distance
  in mm by 972.

  This refutes the earlier `0.420 m -> 265` reading that made the mapping
  look unresolved. That pair was never reproduced; these two were measured
  against the gateway's own record of what it served, four minutes apart,
  and agree to the millimetre.
* **counter channel** — a tipping-bucket count must only ever increase. Any
  decrease is a provably wrong decode, and needs no reference file to detect

---

## Things that will bite

**The keep-alive is a mitigation, not a guarantee.** If the console session
drops anyway, reconfiguration stops and the test continues read-only — the
SDR keeps logging whatever the rig is sending. That is still useful data, but
no further protocol or ID changes will land. The events log will say so.

**`--dedup` must stay well below the transmit interval.** It guards against
one burst being decoded in two overlapping windows. Set it too high and
genuine consecutive bursts carrying the same value are silently dropped —
which is precisely the bug that was fixed in the original script.

**Bit 12 is not part of the ID.** The decoders fold it away; the soak log
keeps `id_folded` and `id_bit12` separately. On this rig it appears to signal
HELD/STALE — eleven frozen frames had it set, live ones clear — and that
inference deserves confirming or refuting during this test.

**A frozen payload is not necessarily a radio fault.** Last time the rig
transmitted a byte-identical message for 45 minutes; the cause was upstream,
in a validity band that was rejecting every reading. Check `sdi12_served.csv`
before suspecting the transmitter.
