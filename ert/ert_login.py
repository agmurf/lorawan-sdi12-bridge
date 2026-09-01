#!/usr/bin/env python3
"""
Log the ERT-A2 console in, and leave it where the keepalive can hold it.

    python3 /home/pi/ert_login.py              # normal login
    python3 /home/pi/ert_login.py --recover    # unstick a quiet console

WHAT IT DOES NOT DO
The password is the operator's to type, in their own terminal. This script
refuses to run without a TTY, takes no --password argument, reads no
environment variable, and uses getpass so the value is never echoed.

THE FAILURE THIS VERSION EXISTS TO FIX
The previous version classified a stale '*' left over from an earlier run
as "still authenticating", entered its wait branch, and NEVER PROMPTED.
The operator typed the password into a script that was not asking for it,
so it went to the shell -- echoed in plain text on screen, and never sent
to the unit at all. Three lessons, all encoded below:

  * An EMPTY buffer is not "still authenticating", it is "the console is
    silent". Those need different handling and had been conflated.
  * '*' echo only means "still authenticating" IMMEDIATELY AFTER we send a
    password. Recalled from a previous run it means nothing of the kind, so
    the echo state is now reachable only via the password step.
  * A script that is not going to prompt must EXIT, not sit silently.
    Waiting in silence is indistinguishable from waiting for input, and the
    operator will reasonably type into it.

WHY --recover EXISTS, AND WHY IT IS NOT AUTOMATIC
This console does not repaint unsolicited, so a quiet port is genuinely
ambiguous: the unit may be at a password field holding characters, or
somewhere else entirely. A bare CR resolves it -- at a password field it
submits a wrong password, the login fails harmlessly, and the unit returns
to the User Type menu, which is a known state.

That is safe IF we are at a password field and unsafe if we are in a
configuration wizard, where a CR accepts a default. The script cannot tell
the two apart, so it does not decide: --recover is the operator saying
"I have looked, I believe this is a password prompt, submit it". The
judgement stays with the person who can see the rig.
"""
import argparse
import getpass
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ert                                                   # noqa: E402

USER_TYPE_RE = re.compile(r"User Type", re.I)
ECHO_ONLY_RE = re.compile(r"^[*\s]*$")


def classify(screen):
    """Name the screen. 'quiet' and 'echo' are deliberately distinct."""
    if not screen.strip():
        return "quiet"
    tail = screen.rstrip()[-400:]
    if ert.MAIN_MENU_RE.search(tail):
        return "main"
    if ert.is_credential_prompt(screen):
        return "password"
    if USER_TYPE_RE.search(screen):
        return "usertype"
    if ECHO_ONLY_RE.match(screen):
        return "echo"
    return "unknown"


def settle(s, timeout, quiet=1.2):
    """Accumulate reads until a screen is recognisable, or timeout."""
    buf = ""
    deadline = time.time() + timeout
    while time.time() < deadline:
        buf += ert.drain(s, quiet)
        if classify(buf) in ("main", "password", "usertype"):
            break
    return buf, classify(buf)


def send_password(s, pw, delay=0.06):
    """Type the password one character at a time, counting the echo back.

    WHY NOT JUST WRITE THE LINE
    Because the unit drops it. An eight-character password written in one
    call produced a SINGLE asterisk, so seven characters and very likely
    the carriage return never landed -- which is exactly why the login
    appeared to hang with the console printing nothing back. Menu
    keystrokes have always worked, and those are sent one at a time.

    The echo is counted, never recorded: this proves every character
    arrived without the script ever holding what they were. The count is
    compared, not printed, so the password length does not leak either.
    """
    echoed = 0
    for ch in pw:
        s.write(ch.encode("latin-1"))
        s.flush()
        time.sleep(delay)
        echoed += ert.drain(s, 0.15).count("*")
    time.sleep(0.4)
    s.write(b"\r")
    s.flush()
    return len(pw), echoed


def show(screen, label):
    print(f"--- {label} ---")
    print(screen.strip()[-500:] or "(nothing)")
    print("-" * (len(label) + 8))


def do_login(s):
    """User Type -> Engineer -> password -> Main Menu. Returns exit code."""
    screen, verdict = settle(s, 8)

    # The console does not repaint unsolicited, and a PREVIOUS invocation
    # may already have consumed the screen it painted -- which is exactly
    # what --recover does on its way to leaving us at a known menu. ert.py
    # persists the last non-empty screen for this reason; use it.
    #
    # But trust it only where it names a real screen. A recalled '*' is
    # stale password echo and says nothing about where the unit is now;
    # acting on that is what caused the silent-prompt failure.
    if verdict == "quiet":
        remembered = ert.recall()
        rverdict = classify(remembered)
        if rverdict in ("main", "usertype", "password"):
            print("(console silent -- using the last screen it painted)")
            screen, verdict = remembered, rverdict

    show(screen, f"current screen [{verdict}]")

    if verdict in ("quiet", "echo", "unknown"):
        sys.stderr.write(
            "\nThe console is not showing a screen this script can act on.\n"
            "It does not repaint unsolicited, so a quiet port is ambiguous:\n"
            "it may be at a password field holding characters from an\n"
            "earlier attempt, or somewhere else entirely.\n\n"
            "NOT going to guess, and NOT going to sit here silently waiting\n"
            "while you type into a prompt that is not listening.\n\n"
            "If you believe it is at a password prompt, submit it and let\n"
            "the login fail back to the User Type menu:\n"
            "    python3 /home/pi/ert_login.py --recover\n\n"
            "If you would rather look first, watch it live:\n"
            "    picocom -b 115200 /dev/ttyACM0     (Ctrl-A Ctrl-X to exit)\n")
        return 1

    for _ in range(6):
        if verdict == "main":
            ert.remember(screen)
            print("\nAt the Main Menu. Restart the keepalive to hold it:")
            print("    sudo systemctl start ert-keepalive.timer")
            return 0

        if verdict == "usertype":
            print("\nAt the User Type menu; selecting Engineer.")
            ert.mark_active()
            s.write(b"a")
            screen, verdict = settle(s, 10)
            ert.remember(screen)
            show(screen, f"after selecting Engineer [{verdict}]")
            continue

        if verdict == "password":
            pw = getpass.getpass("ERT-A2 password (not echoed): ")
            ert.mark_active()
            sent, echoed = send_password(s, pw)
            del pw
            if echoed < sent:
                print("  [!] the unit acknowledged fewer characters than "
                      "were sent -- it is dropping input. Retrying more "
                      "slowly may help; the password itself is not the "
                      "problem here.")
            else:
                print("  every character was acknowledged by the unit.")
            # Only HERE does '*' echo mean "still authenticating", and only
            # here do we keep waiting through it.
            screen, verdict = settle(s, 25)
            if verdict == "echo":
                extra, verdict = settle(s, 15)
                screen += extra
            ert.remember(screen)
            show(screen, f"after login [{verdict}]")
            if verdict in ("quiet", "echo"):
                sys.stderr.write(
                    "\nThe unit accepted the keystrokes but has printed\n"
                    "nothing back. It may have rejected the password without\n"
                    "a message. Try --recover to force it back to the User\n"
                    "Type menu, then log in again.\n")
                return 1
            continue

        sys.stderr.write(
            "\nUnrecognised screen; guessing here could accept a "
            "configuration default. Stopping. Look with:\n"
            "    picocom -b 115200 /dev/ttyACM0\n")
        return 1

    sys.stderr.write("\nGave up without reaching the Main Menu.\n")
    return 1


def do_recover(s):
    print("Sending a bare CR to submit whatever is in the field.")
    print("At a password prompt this fails the login and returns the unit")
    print("to the User Type menu. That is the intended outcome.\n")
    ert.mark_active()
    s.write(b"\r")
    screen, verdict = settle(s, 15)
    ert.remember(screen)
    show(screen, f"after CR [{verdict}]")
    if verdict in ("usertype", "password", "main"):
        print("\nBack at a known screen. Now run:")
        print("    python3 /home/pi/ert_login.py")
        return 0
    print("\nStill not showing a recognisable screen. Look at it directly:")
    print("    picocom -b 115200 /dev/ttyACM0     (Ctrl-A Ctrl-X to exit)")
    return 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--recover", action="store_true",
                    help="submit a bare CR to unstick a quiet console. Use "
                         "only when you believe it is at a password prompt: "
                         "the login then fails harmlessly back to the User "
                         "Type menu.")
    a = ap.parse_args()

    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        sys.stderr.write(
            "ert_login.py: refusing to run without a terminal.\n"
            "The password must be typed by a person, at their own keyboard.\n")
        return 2

    for bad in ("ERT_PASSWORD", "ERT_PASS", "PASSWORD"):
        if os.environ.get(bad):
            sys.stderr.write(f"ert_login.py: {bad} is set; this script "
                             f"deliberately ignores it. Unset it.\n")
            return 2

    s = ert.open_port()
    try:
        return do_recover(s) if a.recover else do_login(s)
    except KeyboardInterrupt:
        sys.stderr.write("\ninterrupted; nothing further sent.\n")
        return 130
    finally:
        s.close()


if __name__ == "__main__":
    raise SystemExit(main())
