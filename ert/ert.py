#!/usr/bin/env python3
"""
Console driver for the ELPRO ERT-A2 over its STM32 USB virtual COM port.

WHY A SCRIPT RATHER THAN A TERMINAL
The ERT-A2 is a live flood telemetry unit. Driving its menus from a script
makes every keystroke explicit and puts both the keys sent and the unit's
reply in the log, which an interactive terminal does not.

WHAT IT WILL NOT DO
It refuses to transmit while the unit is sitting at a credential prompt.
Passwords are the operator's to type, in their own terminal.

THE GUARD MUST REMEMBER, BECAUSE A CONSOLE IS A STREAM AND NOT A SCREEN
An earlier version checked only the bytes that arrived during this
invocation. That is wrong and it failed in exactly the way you would
expect: the run that pressed `a` consumed the `Password:` prompt along
with it, so the NEXT run read an empty buffer, concluded all was well,
and typed a character straight into the password field.

Re-reading cannot recover the prompt -- the unit already sent it and will
not send it again unsolicited. So the last non-empty screen is persisted
to STATE_FILE and the guard falls back to it whenever nothing new has
arrived. Forgetting where you are must never be mistaken for being
somewhere safe.

Nor can the guard simply send a CR to redraw and find out: at a password
prompt a CR SUBMITS the field. That is why --nudge is itself gated by the
guard rather than being a way around it. There is deliberately no
override flag -- an escape hatch here would defeat the only thing this
guard is for.

DTR/RTS ARE LEFT DEASSERTED
On STM32 virtual COM ports asserting DTR can reset the target. Both are
set false before open() so that merely opening the port cannot disturb a
unit that may be mid-transmission on the ALERT radio.
"""
import argparse
import os
import re
import sys
import time

import serial

PORT = "/dev/ttyACM0"
BAUD = 115200

# Beside the script, NOT under ~. This is run both as pi and, via the
# remote agent, as root. A per-user path silently gives each identity its
# own blank memory, and a guard that has forgotten where it is will let
# anything through -- which is exactly how it failed the first time.
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          ".ert_last_screen")

# A credential prompt is the LAST thing the unit said, so this is anchored
# to the end of the buffer. Matching "password" anywhere would fire on the
# Main Menu's own "e) Change Password" entry and lock the driver out of
# the entire device -- a guard that blocks everything gets switched off,
# and then it protects nothing.
# The Main Menu, and only the Main Menu, is safe to send a bare CR to.
# Anywhere else a CR either submits a password or accepts a wizard default.
MAIN_MENU_RE = re.compile(r"Main Menu:.*Selection:\s*$", re.S)

# Stamped by every real send. Keepalive refuses while it is fresh, so a
# timer can never fire a CR into the middle of someone's reconfiguration.
ACTIVITY_LOCK = "/run/ert-active.lock"
ACTIVITY_LOCK_S = 120


def mark_active():
    try:
        with open(ACTIVITY_LOCK, "w") as fh:
            fh.write(str(time.time()))
    except OSError:
        pass


def recently_active():
    try:
        with open(ACTIVITY_LOCK) as fh:
            return (time.time() - float(fh.read().strip())) < ACTIVITY_LOCK_S
    except Exception:
        return False


CREDENTIAL_PROMPT_RE = re.compile(r"(pass\s?word|pass\s?code|pin)\s*:\s*$",
                                  re.IGNORECASE)


def open_port():
    s = serial.Serial()
    s.port = PORT
    s.baudrate = BAUD
    s.timeout = 0.2
    s.dtr = False
    s.rts = False
    s.open()
    return s


def drain(s, settle=1.5):
    """Read until the unit has stopped talking for `settle` seconds."""
    out = bytearray()
    deadline = time.time() + settle
    while time.time() < deadline:
        chunk = s.read(4096)
        if chunk:
            out += chunk
            deadline = time.time() + settle
    return out.decode("latin-1")


def remember(screen):
    """Persist the last non-empty screen. Blank output means 'nothing new',
    never 'the prompt is gone', so blanks are not recorded."""
    if screen.strip():
        try:
            with open(STATE_FILE, "w") as fh:
                fh.write(screen)
        except OSError:
            pass


def recall():
    try:
        with open(STATE_FILE) as fh:
            return fh.read()
    except OSError:
        return ""


def is_credential_prompt(text):
    """True if the unit is WAITING for a credential right now.

    Only the tail is examined: menus list credential-related options and
    scrollback holds old prompts, neither of which means the cursor is
    sitting in a password field.
    """
    return bool(CREDENTIAL_PROMPT_RE.search(text.rstrip()[-200:]))


def current_screen(fresh):
    """What the unit is showing. Falls back to the remembered screen when
    nothing new arrived -- silence is not evidence of a new state."""
    return fresh if fresh.strip() else recall()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("keys", nargs="?", default=None,
                    help="literal characters to send; omit to read only")
    ap.add_argument("--enter", action="store_true", help="append CR to keys")
    ap.add_argument("--settle", type=float, default=1.5)
    ap.add_argument("--keepalive", action="store_true",
                    help="hold the Engineer session open: send a bare CR, but "
                         "ONLY at the Main Menu and ONLY when no other send "
                         "has happened recently. Safe to run from a timer.")
    ap.add_argument("--nudge", action="store_true",
                    help="send a bare CR first to redraw the current menu "
                         "(refused at a credential prompt, where CR submits)")
    a = ap.parse_args()

    s = open_port()
    try:
        fresh = drain(s, 0.6)
        screen = current_screen(fresh)
        remember(fresh)

        if a.keepalive:
            if recently_active():
                print("keepalive: skipped, console in active use")
                return 0
            if is_credential_prompt(screen):
                sys.stderr.write("keepalive: at a credential prompt -- "
                                 "the session has dropped. Log in again.\n")
                return 2
            if not MAIN_MENU_RE.search(screen.rstrip()[-400:]):
                sys.stderr.write(
                    "keepalive: NOT at the Main Menu, so a CR is not safe "
                    "here. Doing nothing.\n----\n"
                    + screen.strip()[-300:] + "\n----\n")
                return 1
            s.write(b"\r")
            time.sleep(0.4)
            after = drain(s, a.settle)
            remember(after)
            print("keepalive: session held at the Main Menu")
            return 0

        # One guard, checked before ANY write -- nudge included.
        wants_to_write = a.nudge or a.keys is not None
        if wants_to_write and is_credential_prompt(screen):
            sys.stderr.write(
                "REFUSING TO SEND: the unit is at a credential prompt.\n"
                "Passwords are yours to type. Log in, then re-run.\n"
                "----\n" + screen.strip() + "\n----\n")
            return 2

        if a.nudge:
            mark_active()
            s.write(b"\r")
            time.sleep(0.4)
            fresh += drain(s, a.settle)
            screen = current_screen(fresh)
            remember(fresh)
            # The redraw may itself have landed on a credential prompt.
            if a.keys is not None and is_credential_prompt(screen):
                sys.stderr.write(
                    "REFUSING TO SEND: redraw revealed a credential prompt.\n"
                    "----\n" + screen.strip() + "\n----\n")
                return 2

        if a.keys is None:
            print(fresh if fresh.strip() else "(nothing new; last seen)\n" + screen)
            return 0

        # PACE THE WRITE. The unit drops burst serial input: eight
        # characters written in one call arrived as ONE. Menu keystrokes
        # had always worked only because they are single characters, and
        # the fault stayed hidden until a password was sent. Writing a
        # multi-character value -- a station ID, a protocol selection --
        # in one call would silently enter a DIFFERENT value than asked
        # for, on a live flood telemetry unit. Measured, see
        # ert/diagnostics/echo_test.py.
        mark_active()          # keepalive must stand off while we navigate
        for ch in a.keys.encode("latin-1"):
            s.write(bytes([ch]))
            s.flush()
            time.sleep(0.06)
        if a.enter:
            time.sleep(0.2)
            s.write(b"\r")
            s.flush()
        after = drain(s, a.settle)
        remember(after)
        print(fresh + after)
        return 0
    finally:
        s.close()


if __name__ == "__main__":
    raise SystemExit(main())
