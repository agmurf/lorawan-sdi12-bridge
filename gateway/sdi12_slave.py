#!/usr/bin/env python3
"""
SDI-12 slave (sensor) for the Pi, so an ELPRO ERT-A2 can poll it for
River Level and retransmit over ALERT2.

ROLE REVERSAL: the RAK13010 is designed to be an SDI-12 *master* driving
sensors. Here the ERT-A2 is the master/recorder and the Pi is the sensor.
We answer commands; we never initiate.

WIRE FORMAT: 1200 baud, 7 data bits, EVEN parity, 1 stop bit. Single wire,
half duplex -- so everything we transmit also lands in our own receive
buffer and must be discarded (see _drain_echo).

COMMANDS the ERT-A2 is documented to send (manual pp23-25):
    ?!      address query          aM! aMC!    start measurement
    aI!     identification         aC! aCC!    concurrent measurement
    aD0!    send data              aR0! aRC0!  continuous measurement
    aAb!    change address         a!          acknowledge active

TIMING: SDI-12 requires the sensor to begin responding within 15 ms of the
command. Linux userspace is not real-time, so responses are computed from
already-held state (never calculated on demand) and the process asks for
SCHED_FIFO.

DATA SOURCE: a JSON state file written by whatever produces readings
(local_ns_logger.py once the LoRa path is back). This decouples the
timing-critical serial loop from the LoRa decode path entirely.
    {"values": [1.234, 3.85], "updated": 1785808460}
"""
import json
import os
import time

STATE_FILE = os.environ.get("SDI12_STATE", "/home/pi/river_state.json")
PORT = os.environ.get("SDI12_SERIAL", "/dev/serial0")

# Identification fields, padded to the exact widths the SDI-12 spec requires.
SDI12_VERSION = "13"          # v1.3
VENDOR = "HYDROLRA"           # exactly 8
MODEL = "RIVER1"              # exactly 6
SENSOR_VERSION = "100"        # exactly 3

# Values older than this are not trustworthy. SDI-12 has no "bad data"
# concept, so we substitute a sentinel the ERT-A2 side can filter on.
#
# -9.999 matches em411_height.INVALID_SENTINEL. It is scaled x1000 by the
# ERT-A2, reaching the radio as -9999 mm -- the conventional hydrology
# no-data value. -9999.0 would have become -9,999,000 mm.
MAX_AGE_S = int(os.environ.get("SDI12_MAX_AGE", "900"))
STALE_SENTINEL = -9.999

# Characters that can legally begin an SDI-12 command: the sensor
# address (0-9, A-Z, a-z) or the "?" of an address query. Anything
# else leading the frame is line noise and is skipped.
ADDRESS_CHARS = frozenset(
    "0123456789"
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "abcdefghijklmnopqrstuvwxyz"
    "?")

CR_LF = "\r\n"


def sdi12_crc16(text):
    """Raw CRC value. This is CRC-16/ARC: reflected poly 0xA001, init 0.
    Being a standard algorithm means it can be checked against the usual
    "123456789" -> 0xBB3D vector rather than taken on trust."""
    crc = 0
    for ch in text:
        crc ^= ord(ch)
        for _ in range(8):
            if crc & 1:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
    return crc


def sdi12_crc(text):
    """SDI-12 CRC as the spec's 3 printable ASCII characters."""
    crc = sdi12_crc16(text)
    return (chr(0x40 | (crc >> 12))
            + chr(0x40 | ((crc >> 6) & 0x3F))
            + chr(0x40 | (crc & 0x3F)))


def format_value(v):
    """SDI-12 value: sign always present, max 7 digits, optional point."""
    s = f"{v:+.3f}"
    # Trim to the spec's 9-character ceiling (sign + 7 digits + point)
    if len(s) > 9:
        s = f"{v:+.1f}"[:9]
    return s


class Sdi12Slave:
    """Pure protocol logic. handle() takes a command, returns a response
    string (or None to stay silent). No I/O -- which is what makes the
    whole thing testable with no hardware attached."""

    def __init__(self, address="0", state_path=STATE_FILE, clock=time.time,
                 meas_secs=None):
        self.address = address
        self.state_path = state_path
        self.clock = clock
        # ttt in the aM! reply. 0 means "ready now, no service request",
        # which is valid SDI-12 and the simplest path. If the ERT-A2 turns
        # out to dislike it, set SDI12_MEAS_SECS=1 and the serial loop will
        # send a proper service request instead. Configurable rather than
        # hardcoded because this is exactly the kind of interop detail that
        # only shows up against the real recorder.
        if meas_secs is None:
            meas_secs = int(os.environ.get("SDI12_MEAS_SECS", "0"))
        self.meas_secs = max(0, min(999, meas_secs))
        self._pending = []       # values captured at aM!/aC! time
        self.service_request_due = None   # set by handle(), read by the loop

    # ---- data ----------------------------------------------------------
    def read_values(self):
        """Latest reading, or the stale sentinel. Never raises."""
        try:
            with open(self.state_path) as fh:
                st = json.load(fh)
            vals = [float(v) for v in st.get("values", [])]
            updated = float(st.get("updated", 0))
        except (OSError, ValueError, TypeError):
            return [STALE_SENTINEL], True
        if not vals:
            return [STALE_SENTINEL], True
        if self.clock() - updated > MAX_AGE_S:
            return [STALE_SENTINEL] * len(vals), True
        return vals, False

    # ---- protocol ------------------------------------------------------
    def handle(self, cmd):
        # Resynchronise before parsing. The break-to-marking edge that
        # opens an SDI-12 frame can be sampled as a spurious character --
        # 0x7F most often, because the 7-bit mask turns an all-ones
        # glitch into DEL. The ERT-A2 really does send '0M!', and
        # reading that first byte as the address made us ignore a
        # perfectly good command. The recorder cannot tell silence from a
        # dead sensor, so it cost us a poll every time it happened.
        #
        # Take everything up to the FIRST '!' so trailing noise cannot
        # invalidate a good command either.
        cut = cmd.find("!")
        if cut == -1:
            return None
        body = cmd[:cut]

        # Skip ONLY characters that cannot be an address. If a glitch
        # ever produced a valid address character we still refuse the
        # command, exactly as before: being deaf to a corrupt frame is
        # safe, answering one meant for another sensor is not.
        while body and body[0] not in ADDRESS_CHARS:
            body = body[1:]
        if not body:
            return None

        # Address query -- every sensor on the bus answers.
        if body == "?":
            return self.address + CR_LF

        addr, rest = body[0], body[1:]
        if addr != self.address:
            return None            # not for us: stay silent

        # a!  acknowledge active
        if rest == "":
            return self.address + CR_LF

        # aI!  identification
        if rest == "I":
            return (self.address + SDI12_VERSION + VENDOR + MODEL
                    + SENSOR_VERSION + CR_LF)

        # aAb!  change address
        if rest.startswith("A") and len(rest) == 2:
            new = rest[1]
            if new.isalnum():
                self.address = new
                return new + CR_LF
            return None

        # aV!  verification -- report zero values, no diagnostics to give
        if rest == "V":
            return f"{self.address}0000" + CR_LF

        # aM! aMC! aM1..9! aMC1..9!  start measurement
        if rest.startswith("M"):
            crc = rest[1:2] == "C"
            tail = rest[2:] if crc else rest[1:]
            if tail and not tail.isdigit():
                return None
            vals, _ = self.read_values()
            self._pending = vals
            if self.meas_secs:
                self.service_request_due = self.clock() + self.meas_secs
            return f"{self.address}{self.meas_secs:03d}{len(vals)}" + CR_LF

        # aC! aCC! aC1..9!  concurrent measurement (2-digit count)
        if rest.startswith("C"):
            crc = rest[1:2] == "C"
            tail = rest[2:] if crc else rest[1:]
            if tail and not tail.isdigit():
                return None
            vals, _ = self.read_values()
            self._pending = vals
            return f"{self.address}{self.meas_secs:03d}{len(vals):02d}" + CR_LF

        # aR0..9! aRC0..9!  continuous -- data returned directly
        if rest.startswith("R"):
            crc = rest[1:2] == "C"
            tail = rest[2:] if crc else rest[1:]
            if tail and not tail.isdigit():
                return None
            vals, _ = self.read_values()
            return self._data_response(vals, crc)

        # aD0..9!  send data
        if rest.startswith("D") and len(rest) == 2 and rest[1].isdigit():
            idx = int(rest[1])
            vals = self._pending if self._pending else self.read_values()[0]
            if idx > 0:
                # Everything fits in D0, so later blocks are empty.
                return self.address + CR_LF
            return self._data_response(vals, crc=False)

        return None

    def _data_response(self, vals, crc):
        payload = self.address + "".join(format_value(v) for v in vals)
        if crc:
            payload += sdi12_crc(payload)
        return payload + CR_LF


# =====================================================================
# RAK13010 transceiver control
#
# From RAK's own driver (RAK13010-SDI12, examples/RAK13010_SDI_12_Slave):
#     #define TX_PIN  WB_IO6      // SDI-12 data bus
#     #define RX_PIN  WB_IO5      // SDI-12 data bus
#     #define OE      WB_IO4      // Output enable, ACTIVE LOW
#     pinMode(WB_IO2, OUTPUT); digitalWrite(WB_IO2, HIGH);   // power
#
# The line driver is a 74LVC1G240: inverting, with a 3-state output. OE
# gates that output, so it must be pulled LOW while we transmit and
# released HIGH the rest of the time, or the recorder never hears us.
# The inversion is what SDI-12 signalling requires, so the Pi's normal
# UART polarity is already correct -- no software inversion needed.
#
# GPIO numbers are configurable because the RAK6421's IO-to-BCM mapping
# for Slot 2 is not something I could confirm from RAK's documentation.
# =====================================================================
PWR_GPIO = int(os.environ.get("SDI12_PWR_GPIO", "26"))   # IO2, active high
OE_GPIO = int(os.environ.get("SDI12_OE_GPIO", "24"))     # IO4, active low


class Transceiver:
    """Holds the module powered and flips OE around each transmission."""

    def __init__(self, pwr=PWR_GPIO, oe=OE_GPIO, verbose=True):
        self.ok = False
        try:
            import gpiod
            self.chip = gpiod.Chip("gpiochip0")
            self.pwr = self.chip.get_line(pwr)
            self.oe = self.chip.get_line(oe)
            self.pwr.request(consumer="sdi12", type=gpiod.LINE_REQ_DIR_OUT,
                             default_vals=[1])          # power on
            self.oe.request(consumer="sdi12", type=gpiod.LINE_REQ_DIR_OUT,
                            default_vals=[1])           # receive (disabled)
            self.ok = True
            if verbose:
                print(f"[sdi12] transceiver: power GPIO{pwr}=1, "
                      f"OE GPIO{oe} (active low)", flush=True)
        except Exception as e:
            print(f"[sdi12] WARNING: no transceiver control ({e}). "
                  "Replies will not reach the bus.", flush=True)

    def transmit(self):
        if self.ok:
            self.oe.set_value(0)
            time.sleep(0.001)     # let the buffer enable before the start bit

    def receive(self):
        if self.ok:
            self.oe.set_value(1)


# =====================================================================
# Serial transport -- only used on real hardware.
# =====================================================================
def run_serial(address="0", verbose=True):
    import serial

    try:
        os.sched_setscheduler(0, os.SCHED_FIFO, os.sched_param(40))
        if verbose:
            print("[sdi12] SCHED_FIFO acquired", flush=True)
    except (PermissionError, OSError) as e:
        print(f"[sdi12] WARNING: no real-time priority ({e}). "
              "Responses may miss the 15ms SDI-12 deadline under load.",
              flush=True)

    xcvr = Transceiver(verbose=verbose)
    slave = Sdi12Slave(address=address)
    ser = serial.Serial(PORT, 1200,
                        bytesize=serial.SEVENBITS,
                        parity=serial.PARITY_EVEN,
                        stopbits=serial.STOPBITS_ONE,
                        timeout=0.05)
    print(f"[sdi12] listening on {PORT} @1200 7E1 as address '{address}'",
          flush=True)
    print(f"[sdi12] state file {slave.state_path}, "
          f"meas_secs={slave.meas_secs}", flush=True)

    buf = ""
    while True:
        # Service request: only used when SDI12_MEAS_SECS > 0. Tells the
        # recorder the measurement it asked for is now ready.
        if (slave.service_request_due is not None
                and time.time() >= slave.service_request_due):
            slave.service_request_due = None
            msg = slave.address + CR_LF
            _send(ser, xcvr, msg)
            if verbose:
                print(f"[sdi12] service request -> {msg!r}", flush=True)

        chunk = ser.read(32)
        if not chunk:
            continue
        for byte in chunk:
            # A break arrives as a NUL with a framing error; harmless to
            # drop, and we never sleep so we don't need it to wake us.
            if byte in (0x00, 0x0D, 0x0A):
                continue
            ch = chr(byte & 0x7F)
            buf += ch
            if ch == "!":
                t_cmd = time.monotonic()
                cmd, buf = buf, ""
                resp = slave.handle(cmd)
                if resp:
                    lat_ms = (time.monotonic() - t_cmd) * 1000.0
                    _record_latency(lat_ms)
                    _send(ser, xcvr, resp)
                    if verbose:
                        flag = "" if lat_ms < 15.0 else "  *** OVER 15ms ***"
                        print(f"[sdi12] {cmd!r} -> {resp!r}  "
                              f"{lat_ms:.1f}ms{flag}", flush=True)
                elif verbose:
                    print(f"[sdi12] {cmd!r} -> (silent)", flush=True)
            elif len(buf) > 16:
                buf = ""       # runaway, resynchronise


_LAT = {"n": 0, "over": 0, "max": 0.0, "sum": 0.0}


def _record_latency(ms):
    _LAT["n"] += 1
    _LAT["sum"] += ms
    if ms > _LAT["max"]:
        _LAT["max"] = ms
    if ms >= 15.0:
        _LAT["over"] += 1
    # Periodic summary: the distribution matters more than any one reply.
    if _LAT["n"] % 20 == 0:
        avg = _LAT["sum"] / _LAT["n"]
        pct = 100.0 * _LAT["over"] / _LAT["n"]
        print(f"[sdi12] LATENCY over {_LAT['n']} replies: "
              f"avg {avg:.1f}ms  max {_LAT['max']:.1f}ms  "
              f"missed 15ms deadline {_LAT['over']} times ({pct:.1f}%)",
              flush=True)


def _send(ser, xcvr, msg):
    """Enable the line driver, transmit, then hand the bus back.

    OE must be low for the whole transmission and released afterwards,
    otherwise we hold the single-wire bus and the recorder cannot reply.
    """
    xcvr.transmit()
    try:
        ser.write(msg.encode("ascii"))
        ser.flush()
        _drain_echo(ser, len(msg))
    finally:
        xcvr.receive()


def _drain_echo(ser, n):
    """Single-wire bus: our own transmission may come back. Discard it.

    Whether it does depends on whether the 74LVC1G240 gates the receive
    path while transmitting -- untested against real hardware -- so this
    tolerates seeing nothing rather than blocking on it.
    """
    deadline = time.time() + 0.3
    got = 0
    while got < n and time.time() < deadline:
        d = ser.read(n - got)
        if d:
            got += len(d)


if __name__ == "__main__":
    import sys
    addr = sys.argv[1] if len(sys.argv) > 1 else "0"
    run_serial(address=addr)
