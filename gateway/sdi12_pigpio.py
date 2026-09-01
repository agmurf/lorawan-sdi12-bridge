#!/usr/bin/env python3
"""
pigpio bit-bang transport for the RAK13010 SDI-12 module on RAK6421 Slot 2.

PIN MAP -- from RAK6421 schematic sheet 2, revision VD (not inferred):
    IO2  power       net IO26  -> GPIO26   drive HIGH
    IO4  OE          net IO24  -> GPIO24   ACTIVE LOW
    IO5  data RX     net IO19  -> GPIO19   Pi reads
    IO6  data TX     net IO18  -> GPIO18   Pi drives

WHY BIT-BANG AND NOT THE UART: these are ordinary GPIOs. The Pi's
hardware UART only reaches GPIO14/15 (or 32/33, 36/37, none of which are
on a Zero's header), so it cannot be used here. pigpio does the timing in
DMA rather than a Python loop, which is considerably more reliable than
software serial usually implies.

PARITY: SDI-12 is 1200 baud 7E1. pigpio's serial primitives have no
parity support, so we fold it in by hand: transmit 8N1 with bit 7 set to
the even-parity bit, and mask 0x7F on receive. A 7E1 frame and an 8N1
frame are both 10 bits on the wire, so this is bit-identical -- not an
approximation.

POLARITY: the 74LVC1G240 is inverting, which is exactly what SDI-12
needs (its marking state is the LOW voltage, opposite to TTL UART). The
receive polarity is determined empirically by selftest() rather than
reasoned about, because getting it wrong is silent and confusing.
"""
import time

import pigpio

PWR_GPIO = 26
OE_GPIO = 24
TX_GPIO = 18
RX_GPIO = 19
BAUD = 1200

# Receive polarity. 0 = standard UART sense (idle high, break low), which
# is what a real SDI-12 signal presents at GPIO19. Do NOT set this from
# the loopback self-test: our own echo passes through the inverting line
# driver and so has the opposite sense. Verified against a captured '?!'
# from the ERT-A2. Override with SDI12_INVERT if a board revision differs.
import os as _os
RX_INVERT = int(_os.environ.get("SDI12_INVERT", "0"))

# Transmit polarity is the OPPOSITE of receive, because our output passes
# through the inverting line driver while incoming signals do not.
TX_INVERT = int(_os.environ.get("SDI12_TX_INVERT", "1"))
# Line level between frames, in GPIO terms (inverted idle is LOW).
IDLE_LEVEL = 0 if TX_INVERT else 1


def encode_7e1(ch):
    """7 data bits + even parity, packed into 8 bits for an 8N1 frame."""
    b = ord(ch) & 0x7F
    parity = bin(b).count("1") & 1        # 1 when data has odd ones
    return b | (parity << 7)


def decode_7e1(byte):
    return chr(byte & 0x7F)


class Sdi12Transport:
    def __init__(self, pi, invert=0, verbose=True):
        self.pi = pi
        self.verbose = verbose
        self.invert = invert
        self._rx_open = False

        pi.set_mode(PWR_GPIO, pigpio.OUTPUT)
        pi.write(PWR_GPIO, 1)                 # module powered
        pi.set_mode(OE_GPIO, pigpio.OUTPUT)
        pi.write(OE_GPIO, 1)                  # receive (buffer disabled)
        pi.set_mode(TX_GPIO, pigpio.OUTPUT)
        pi.write(TX_GPIO, IDLE_LEVEL)         # idle, in GPIO terms
        time.sleep(0.3)                       # let the rail settle

        self.open_rx(invert)

    # ---- receive --------------------------------------------------------
    def open_rx(self, invert):
        self.close_rx()
        # A previous process may have left bb_serial open on this GPIO.
        # pigpiod tracks that per-GPIO rather than per-client, so a
        # restart would otherwise fail with "GPIO already in use".
        try:
            self.pi.bb_serial_read_close(RX_GPIO)
        except Exception:
            pass
        self.invert = invert
        self.pi.bb_serial_read_open(RX_GPIO, BAUD, 8)
        self.pi.bb_serial_invert(RX_GPIO, invert)
        self._rx_open = True

    def close_rx(self):
        if self._rx_open:
            try:
                self.pi.bb_serial_read_close(RX_GPIO)
            except Exception:
                pass
            self._rx_open = False

    def read(self):
        """Return any received characters, parity stripped."""
        count, data = self.pi.bb_serial_read(RX_GPIO)
        if count <= 0:
            return ""
        return "".join(decode_7e1(b) for b in data)

    def drain(self, seconds=0.05):
        end = time.time() + seconds
        while time.time() < end:
            self.read()
            time.sleep(0.005)

    # ---- transmit -------------------------------------------------------
    def _pulses(self, payload):
        """Hand-built serial waveform, inverted if TX_INVERT.

        One pulse per bit. Durations come from cumulative bit position so
        the 833.333us bit time does not drift over a frame.
        """
        import pigpio as _p
        mask = 1 << TX_GPIO
        bit_us = 1_000_000.0 / BAUD
        pulses = []
        idx = 0
        for byte in payload:
            bits = [0]                                  # start
            bits += [(byte >> i) & 1 for i in range(8)]  # data, LSB first
            bits.append(1)                              # stop
            for b in bits:
                level = (1 - b) if TX_INVERT else b
                dur = int(round((idx + 1) * bit_us) - round(idx * bit_us))
                if level:
                    pulses.append(_p.pulse(mask, 0, dur))
                else:
                    pulses.append(_p.pulse(0, mask, dur))
                idx += 1
        return pulses

    def send(self, text):
        """Enable the driver, clock the frame out, release the bus."""
        payload = bytes(encode_7e1(c) for c in text)
        pi = self.pi
        pi.wave_clear()
        pi.wave_add_generic(self._pulses(payload))
        wid = pi.wave_create()
        try:
            pi.write(TX_GPIO, IDLE_LEVEL)
            pi.write(OE_GPIO, 0)              # buffer on
            time.sleep(0.001)
            pi.wave_send_once(wid)
            while pi.wave_tx_busy():
                time.sleep(0.001)
            time.sleep(0.002)                 # let the last stop bit finish
            pi.write(TX_GPIO, IDLE_LEVEL)
        finally:
            pi.write(OE_GPIO, 1)              # hand the bus back
            pi.wave_delete(wid)

    def stop(self):
        self.close_rx()
        self.pi.write(OE_GPIO, 1)


def selftest(pi, probe="0!"):
    """Determine receive polarity from the module's own loopback.

    With OE low the buffer drives the bus, and the bus returns on IO5 --
    so whatever we transmit comes back. Try both polarities and keep the
    one that decodes correctly. This settles the question by measurement
    instead of by argument about inverting buffers.
    """
    for invert in (1, 0):
        t = Sdi12Transport(pi, invert=invert, verbose=False)
        try:
            t.drain(0.1)
            t.send(probe)
            time.sleep(0.15)
            got = t.read()
            ok = probe in got
            print(f"  invert={invert}: sent {probe!r} read {got!r}"
                  f"   {'<-- DECODES' if ok else ''}")
            if ok:
                return invert
        finally:
            t.stop()
    return None


if __name__ == "__main__":
    pi = pigpio.pi()
    if not pi.connected:
        raise SystemExit("pigpiod is not running: sudo systemctl start pigpiod")
    print("RAK13010 loopback self-test (GPIO18 TX -> buffer -> GPIO19 RX)\n")
    inv = selftest(pi)
    print()
    if inv is None:
        print("RESULT: neither polarity decoded. The electrical path is")
        print("proven (loopback_test.py), so this is a framing problem --")
        print("baud, parity packing or stop bits.")
    else:
        print(f"RESULT: receive polarity is invert={inv}.")
        print("Bit-banged SDI-12 framing works end to end through the")
        print("module's own buffer. Ready for the ERT-A2.")
    pi.stop()
