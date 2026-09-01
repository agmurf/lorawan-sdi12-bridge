# ERT-A2 baseline configuration

Captured 1 Sep 2026, 18:41 AEST, from `g) Show/Save Configuration`.
**Restore to this after the protocol matrix.**

    Communication Mode : Integrated Radio Reporting
    Unit Type          : Field Station
    Station Address    : 4078
    Tx Frequency       : 151.500000 MHz      Tx Power: 5W
    Report Format      : ALERT IFLOWS
    GPS Update         : 1440                GPS Maximum On: 2
    Update Time        : 5min                Paralysis Time: 6sec
    Data Log Format    : ALERT2 ASCII
    Firmware           : v1.7                Hardware: Rev4.A

    External Inputs        DI3          Internal        Battery
      Mode    Pulse                       ID      4080
      ID      4078                        Format  U-2
      Format  U-2                         Value   12.1 V
      Scale   1
      Offset  0mm
      Sens    1mm
      Value   528mm

    SDI-12 Operating Mode : Controller
    SDI Sample Time       : 5min      SDI Warmup Time: 20sec
    SDI Measurement CMD   : Start Measurement (M!)

    SDI Sensor 013HYDROLRARIVER1100
      Chan 0
      ID      4079
      Format  F-4          <- IEEE-754 float32
      Scale   1000         <- corroborates raw = metres x 1000
      Offset  0mm
      Sens    10mm
      Value   615mm

## What this settled

**4078 is not cross-talk.** Its ID is the unit's own Station Address, and
it is the DI3 pulse counter. `Raw 528` is a frozen accumulator -- frozen
because the relay is disconnected, which is exactly right. Three readings
over 45 minutes, all 528, monotonic. The single-bit distance from 4079
remains a hazard worth designing out, but nothing observed is a
misdecode.

**`Scale 1000` confirms the encoding independently.** The mapping was
measured off-air first (7 pairs, residuals 0.00 mm) and the unit's own
configuration agrees. Two independent routes to the same answer.

## Open, and worth attention

**The clock is five years wrong**: the console reports `15-Sep-2021`.
`GPS Update: 1440` with `GPS Maximum On: 2` suggests the GPS is not
achieving lock. ALERT2 is a time-based protocol, so this is a plausible
cause of the item below and should be settled before reading anything
into the ALERT2 leg of the test.

**A forced update was generated but not received.** `d) e) Force Update
Message` at 18:42:46 produced:

    |==> ID=4078; val=528
    |==> ID=4080; val=121
    |==> ID=4079; val=1142538240        <- 0x44198000, float32 614.0
    sent - errts_send_to_radio: ALERT2B: 414C4552543211010F746D2EEF4F6700F00F7900EE4F1000
    ALERT2 transmission finished after 0 seconds awake

The SDR heard nothing at that moment, while every scheduled report either
side of it arrived at ~68 dB and decoded cleanly as Enhanced iFLOWS. So
the forced path emits an **ALERT2** frame where the scheduled path emits
iFLOWS, and `0 seconds awake` suggests the radio may not have been keyed.
Not yet explained; do not assume the forced path is equivalent to a real
transmission when testing.

---

# Console handling: things that cost time, 1 Sep 2026

## The unit drops burst serial input

Eight characters written in one call arrive as **one**. Measured, both
directions, see `ert/diagnostics/echo_test.py`:

    ONE WRITE    sent 8 -> echoed 1
    PACED 60ms   sent 8 -> echoed 8

Every write is now paced at 60 ms/character. This hid for a long time
because menu keystrokes are single characters and always worked; it only
surfaced when a password was sent, where it looked like a hung login or a
wrong password. It was neither.

## Numeric fields are FIXED WIDTH and left-aligned

`SDI Sample Time` is two digits wide. Typing `5` yields **50**, not 5.
Typing `05` yields 5. This was diagnosed only after producing 50 twice and
noticing it was a pattern rather than a slip.

Wider fields (`Scaling`, accepting `1000`) take normal input, so this is
per-field. **Always read the value back from `Show/Save Configuration`
before trusting an edit**, and never assume a committed field holds what
was typed.

## Fields arrive PRE-FILLED with the cursor after the value

Typing appends: `4079` + `4091` = `40794091`, outside the accepted range.
Clear with backspaces first, watching for the BEL that means the field is
already empty.

## Some fields accept no input at all

`Set Date & Time` echoes every character then erases it with
backspace-space-backspace and rings a BEL. Digits, separators, everything.
The clock is GPS-disciplined and there is no manual entry anywhere in the
menus. A field displaying a value does **not** imply it is editable.

## Unsaved changes do not survive a USB re-enumeration

The unit reloads its SAVED configuration when the USB link resets. During
one session the link re-enumerated ten times and every unsaved change was
lost, twice, while the ALERT2 Quick Setup's values persisted -- because
that had been saved.

Declining to save was a deliberate safety choice while the saved config
was still the known-good baseline. Once the Quick Setup had been saved
over it, that reasoning inverted: refusing to save protected nothing and
guaranteed the work evaporated. **Save after each coherent change**, and
keep the baseline in this file rather than in the unit.

## A dropped session looks exactly like a menu

When the USB reset killed the Engineer session mid-navigation, the
remaining keystrokes went into the login prompt that had replaced the
menu -- which is how `Invalid Password:` appeared on the console. Every
navigation step now checks for a credential prompt BEFORE sending, and
aborts. Asserting only the destination screen is not enough on a link that
can drop between steps.

## ALERT2 I/O Quick Setup rewrites more than it says

Running it changed `Station Address` to 16569, reassigned every sensor ID
to the small ALERT2 scheme (0 / 7 / 8), set `Report Format` to ALERT2,
enabled an `AI1` 4-20mA channel with nothing connected (reading -1250mm,
which it will transmit as a real value), **disabled SDI-12 entirely**, set
`SDI Sample Time` to 30min, and blanked the river channel to `Scale 0,
Sens 0`. `Scale 0` is the dangerous one: the channel keeps reporting and
transmits zeros regardless of what SDI-12 returns.
