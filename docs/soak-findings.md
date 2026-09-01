# Three-protocol soak — findings

River-only run, 1 Sep 2026. Relay on DI1 disconnected, so no counter leg.

---

## 1. The value encoding is solved

**`raw = metres x 1000`**, offset zero, residuals **0.00 mm** across seven
matched pairs, match ages 0-286 s (inside the 5-minute poll interval).

    17:49:56  474 <- 0.474      18:30:11  585 <- 0.585
    17:54:57  500 <- 0.500      18:35:18  607 <- 0.607
    18:19:56  534 <- 0.534      18:39:51  607 <- 0.607
    18:25:19  561 <- 0.561

Competing candidates are nowhere near: Level x100 is out by a mean 497,
raw distance in mm by 841.

Confirmed independently by the unit's own configuration: the river channel
is `Format F-4`, `Scale 1000`. Measured off-air and declared on the
console agree exactly.

This **refutes** the earlier `0.420 m -> 265` reading that had the mapping
recorded as unresolved. That pair was never reproduced.

---

## 2. ALERT Binary produced a confidently wrong reading

The protocol switch is proven on air -- the decoder's format
identification flipped from `ENHANCED_IFLOWS` to `BINARY` across the
change at 18:49:36. But under ALERT Binary:

    18:50:17   decoded 1946   served 0.666   WRONG -- 1.946 m vs 0.666 m
    18:52:53   decoded  666   served 0.666   correct

The bad reading carried **16 votes**, the same consensus as every correct
one, so vote count did not separate them. ALERT Binary has **no CRC**
(`crc_ok` is `None` by construction), where Enhanced iFLOWS carries a
6-bit one.

Across the run: Enhanced iFLOWS gave 9 river readings, all correct.
ALERT Binary gave 2, one of them wrong by 1.28 m.

**This matters for a flood path.** A single burst decoding 1.946 m
instead of 0.666 m is the difference between quiet and an alarm, with
nothing in the frame to catch it. Two readings is a small sample and this
should not be over-read -- but it is the only error observed all day, and
it appeared in the one format with no error detection.

Do not treat the sample size as settled. Collect more before drawing a
conclusion about rates.

---

## 3. 4078 is not cross-talk

Its ID **is the unit's own Station Address**, and it is the DI3 pulse
counter. `Raw 528` is a frozen accumulator -- frozen because the relay is
disconnected, which is correct behaviour. Four readings across 45
minutes, all 528, monotonic.

The single-bit distance from 4079 remains a hazard worth designing out,
but nothing observed was a misdecode of one for the other. No cross-talk
was detected in the whole run: every burst was claimed by exactly one
protocol.

---

## 4. The clock is five years wrong and cannot be set from the console

The console reports **15-Sep-2021**. `f) Set Date & Time` displays the
current value and then **rejects every character typed** -- each one is
echoed, erased with backspace-space-backspace, and answered with a BEL.
Digits, separators, all of it. There is no time setting anywhere in
`Unit Config` either.

With `GPS Update: 1440` and `GPS Maximum On: 2`, the clock is
GPS-disciplined, and the wrong date means **the GPS is not achieving
lock**. That is an antenna or siting problem at the rig, not something
fixable over the console.

ALERT2 is a time-based protocol, so this is worth settling -- but see the
correction in section 6. The clock was NOT established as the cause of the
ALERT2 silence, and the write-up originally leaned on it too hard.

---

## 5. A forced update was generated but never transmitted

`Unit Diagnostic -> e) Force Update Message` at 18:42:46 produced:

    |==> ID=4078; val=528
    |==> ID=4080; val=121
    |==> ID=4079; val=1142538240        <- 0x44198000, float32 614.0
    sent - errts_send_to_radio: ALERT2B: 414C4552543211010F746D2EEF4F6700F00F7900EE4F1000
    ALERT2 transmission finished after 0 seconds awake

The SDR heard nothing, while every scheduled report either side arrived
at ~68 dB and decoded cleanly. The forced path emits an **ALERT2** frame
where the scheduled path emitted iFLOWS, and `0 seconds awake` suggests
the radio may not have been keyed at all.

**Do not use the forced path as a substitute for a real transmission**
when testing. It is not equivalent, and treating it as such would report
a working chain that never went on air.

---

## Instrumentation gap, deliberately not fixed mid-test

`raw_hex` is empty for every BINARY row. In `frames_multi`, the BINARY
path delegates to `frames_from_symbols`, which returns `(sid, val, fixed,
inv)` and no bytes. `parse_binary` does return them but is not on that
path -- deliberately, because the code records that an independent
reimplementation invented phantom stations 3062 and 7250 on a capture
containing exactly four.

So the 1946 decode cannot be checked against its own bytes. Reconstructing
them from `sid` and `val` would be circular and prove nothing. Fixing this
properly means threading bytes through `frames_from_symbols` without
changing its acceptance behaviour, and that is not a change to make while
a measurement run is in flight.

---

## 6. ALERT2 silence: a correction

Under `ALERT2 Format` (18:55:35 - 19:02:36) nothing was received at all,
and the burst SNR sat at the 13 dB noise floor rather than the 68-70 dB of
a real transmission. So the unit was not keying the radio, as opposed to
transmitting something the decoder could not parse. That much is measured.

**The cause attributed to it was not.** This was written up as tying
"directly to the clock" -- the GPS not locking, ALERT2 being time-slotted,
the forced update reporting `0 seconds awake`. It was a tidy story and it
fitted, which is exactly what made it persuasive.

The operator supplied the actual mechanism: **ALERT2 waits until a
configured number of readings have accumulated and then sends them as a
batch.** Seven minutes at a 5-minute sample interval is simply not enough
readings to trigger a report. No clock fault is required to explain the
silence.

This is the same error this project has made before -- asserting a cause
from a correlation, having correctly observed an effect. The observation
stands; the explanation was invented. The clock is still wrong and still
worth fixing, but it is not established as the cause of anything.

To test ALERT2 properly the batch threshold has to be lowered first,
otherwise the test measures the threshold.

---

## 7. ID change verified on air

Sensor IDs changed at 19:04-19:08, with each digit checked for acceptance
before the next was sent:

    SDI Chan 0 (river)   4079 -> 4091
    DI3                  4078 -> 4090
    Battery              4080    unchanged
    Station Address      4078    unchanged

First transmission afterwards:

    19:10:16   Enhanced iFLOWS   4091 = 717   [not in DB]   68.9 dB   FB BF 66 1D
    19:10:11   served 0.717 m

Exact match, five seconds apart, under a brand-new ID the decoder's sensor
database has never seen. The `not in DB` label is the correct behaviour
and confirms the ID genuinely changed rather than the decoder pattern
matching a known station.

This also removes the single-bit hazard: 4090 and 4091 are still adjacent,
but the river ID is no longer one bit from the unit's own station address.

The config was left **deliberately unsaved** (`Save Current Config? n`),
so the persisted configuration remains the known-good baseline and any
power interruption reverts to it rather than to a test state.

Transmissions stopped entirely while the console sat inside configuration
menus (19:02-19:08) and resumed at 19:10:16 once back at the Main Menu.
Suggestive, not established -- it is equally consistent with the ordinary
5-minute schedule resuming. Worth a controlled check before relying on it.

---

## 8. New configuration verified on air, via SDI-12

After the ALERT2 Quick Setup wiped the SDI-12 channel, it was rebuilt and
this time **saved**:

    SDI-12 Operating Mode : Controller
    SDI Sample Time       : 5min        (Warmup 20sec)
    Chan 0   ID 9   Format F-4   Scale 1000   Sens 10
    Report Format         : ALERT IFLOWS

The full chain -- EM411 -> LoRa -> gateway -> SDI-12 -> ERT-A2 -> VHF --
verified end to end against the gateway's own record of what it served:

    20:07:11   served 0.968 m   sent 968   age 176 s   C9 00 E4 1D
    20:12:16   served 0.989 m   sent 989   age 181 s   C9 80 EE 89

    raw = 1000.0000 * metres + 0.00
    residuals: max 0.00, mean 0.00  (n=2)

Both bursts at ~70 dB, 16 votes, under the new ID 9 that the decoder's
sensor database has never seen. `Level x100` is out by a mean 881.

The gateway is now polled on a clean 5-minute cadence (19:54:15, 19:59:15,
20:04:15, 20:09:15), confirming the sample-time change took effect.

### ALERT2 batching confirmed by direct test

Under `ALERT2 Format` nothing was transmitted for **52 minutes** with the
burst SNR sitting at the 13-15 dB noise floor. Switching the report format
back to `ALERT IFLOWS` -- changing nothing else -- produced a transmission
within five minutes, and every five minutes after.

That is a controlled test rather than an inference: one variable changed,
silence became transmission. It confirms the operator's account that
ALERT2 accumulates readings before sending, and it retires the earlier
guess that the wrong clock was responsible. The clock is still wrong and
still worth fixing; it was never the cause.

### A phantom sensor is on air

The Quick Setup enabled `AI1` as a 4-20mA input with nothing connected.
It is now transmitting as **ID 7**:

    20:12:16   ID 7 = 798

There is no sensor on that input. The value is manufactured from a
floating 4-20mA reading scaled by 5000, and it will be transmitted every
cycle, indistinguishable on air from a real measurement. On a flood
network that is a station reporting a level that does not exist. Disable
`AI1` unless something is actually wired to it.
