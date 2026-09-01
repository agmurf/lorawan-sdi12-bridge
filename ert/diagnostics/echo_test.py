"""Does the ERT-A2 drop burst input? Measured, not assumed.

Sends eight harmless 'X' characters to the PASSWORD field two ways -- one
write, then paced -- and counts the '*' the unit echoes back. No credential
is involved: 'X' is deliberate garbage, and each trial ends with a CR so the
login FAILS and the unit returns to the User Type menu. Nothing is
configured, nothing is committed.

RESULT, 1 Sep 2026:

    ONE WRITE    sent 8 chars -> unit echoed 1     (7 dropped)
    PACED 60ms   sent 8 chars -> unit echoed 8     (all landed)

That is the whole reason every password attempt appeared to hang: one
character arrived and the carriage return did not, so the unit sat waiting
at a half-filled field, printing nothing. The password was never wrong.
Menu keystrokes had always worked because they are sent one at a time.
"""
import sys, time
sys.path.insert(0, "/home/pi")
import ert

PROBE = "XXXXXXXX"

def to_password_prompt(s):
    ert.drain(s, 1.0)
    s.write(b"a"); s.flush()          # Engineer
    scr = ert.drain(s, 2.0)
    return ert.is_credential_prompt(scr), scr

def fail_login(s):
    s.write(b"\r"); s.flush()
    return ert.drain(s, 3.0)

s = ert.open_port()
try:
    print("resetting to a known screen...")
    fail_login(s)
    time.sleep(1)

    for label, paced in (("ONE WRITE", False), ("PACED 60ms", True)):
        ok, scr = to_password_prompt(s)
        if not ok:
            print(f"{label}: could not reach the password prompt; saw:")
            print(repr(scr[-120:]))
            fail_login(s); time.sleep(1)
            continue
        if paced:
            got = 0
            for ch in PROBE:
                s.write(ch.encode()); s.flush()
                time.sleep(0.06)
                got += ert.drain(s, 0.15).count("*")
        else:
            s.write(PROBE.encode()); s.flush()
            got = ert.drain(s, 2.0).count("*")
        print(f"{label:12s} sent {len(PROBE)} chars -> unit echoed {got}")
        fail_login(s)
        time.sleep(1)

    print()
    print("leaving the console at the User Type menu")
    scr = ert.drain(s, 2.0)
    if scr.strip():
        ert.remember(scr)
    print(repr(scr[-160:]))
finally:
    s.close()
