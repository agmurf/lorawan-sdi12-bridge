#!/usr/bin/env python3
"""
SDI-12 slave: tested protocol layer + pigpio bit-bang transport.

    sdi12_slave.Sdi12Slave   protocol, no I/O, 40+ passing tests
    sdi12_pigpio             GPIO18/19 transport via pigpio DMA

Usage:
    sdi12_run.py                 run as a slave, address 0
    sdi12_run.py 3               run as a slave, address 3
    sdi12_run.py --selftest      exercise the whole loop with no ERT-A2

SELFTEST works because the bus is a single wire: anything we transmit
returns on our own receive line. Injecting a command therefore drives the
real path -- TX, buffer, bus, RX, parse, respond -- and is a genuine end
-to-end check rather than another protocol unit test.

TIMING: SDI-12 allows 15 ms from the end of a command to the start of the
reply. Latency is measured from the moment the terminating '!' is seen,
so kernel and pigpio buffering ahead of that is NOT included -- treat the
numbers as a lower bound.
"""
import os
import sys
import time

import pigpio

from sdi12_slave import Sdi12Slave
from sdi12_pigpio import Sdi12Transport, selftest

_LAT = {"n": 0, "over": 0, "max": 0.0, "sum": 0.0}


def record(ms):
    _LAT["n"] += 1
    _LAT["sum"] += ms
    _LAT["max"] = max(_LAT["max"], ms)
    if ms >= 15.0:
        _LAT["over"] += 1
    if _LAT["n"] % 20 == 0:
        print(f"[sdi12] LATENCY over {_LAT['n']} replies: "
              f"avg {_LAT['sum']/_LAT['n']:.1f}ms  max {_LAT['max']:.1f}ms  "
              f"missed 15ms {_LAT['over']} times "
              f"({100.0*_LAT['over']/_LAT['n']:.1f}%)", flush=True)


def main():
    args = [a for a in sys.argv[1:]]
    do_selftest = "--selftest" in args
    args = [a for a in args if not a.startswith("--")]
    address = args[0] if args else "0"

    # Deliberately NOT SCHED_FIFO. pigpiod does the serial timing in DMA;
    # outranking it starves the component that meets the deadline.
    # Measured: SCHED_FIFO gave 1.83 ms worst case, nice -10 gave 0.58 ms
    # under heavier load.
    try:
        os.nice(-10)
        print("[sdi12] priority: nice -10 "
              "(pigpiod left to run unimpeded)", flush=True)
    except (PermissionError, OSError) as e:
        print(f"[sdi12] priority: default ({e})", flush=True)

    pi = pigpio.pi()
    if not pi.connected:
        raise SystemExit("pigpiod not running: sudo systemctl start pigpiod")

    # Polarity is NOT auto-detected. The loopback echo passes through the
    # inverting driver and has the opposite sense to a real incoming
    # signal, so calibrating from it produced a confidently wrong answer.
    # RX_INVERT=0 matches a captured '?!' from the ERT-A2.
    from sdi12_pigpio import RX_INVERT
    print(f"[sdi12] receive polarity invert={RX_INVERT} "
          f"(standard UART sense)", flush=True)
    xport = Sdi12Transport(pi, invert=RX_INVERT, verbose=True)
    slave = Sdi12Slave(address=address)
    print(f"[sdi12] slave address '{address}', "
          f"state {slave.state_path}, meas_secs={slave.meas_secs}",
          flush=True)
    print("[sdi12] GPIO18 TX / GPIO19 RX / GPIO24 OE / GPIO26 power",
          flush=True)

    if do_selftest:
        print("\n[selftest] injecting the ERT-A2's scan sequence\n", flush=True)

    buf = ""
    injected = ["?!", "0I!", "0M!", "0D0!"] if do_selftest else []
    next_inject = time.time() + 1.0

    try:
        while True:
            if injected and time.time() >= next_inject:
                cmd = injected.pop(0)
                print(f"[selftest] --> {cmd}", flush=True)
                xport.send(cmd)
                next_inject = time.time() + 1.5

            if (slave.service_request_due is not None
                    and time.time() >= slave.service_request_due):
                slave.service_request_due = None
                xport.send(slave.address + "\r\n")
                print("[sdi12] service request sent", flush=True)

            data = xport.read()
            if not data:
                time.sleep(0.002)
                continue

            for ch in data:
                if ch in ("\x00", "\r", "\n"):
                    continue          # break artefacts and terminators
                buf += ch
                if ch == "!":
                    t0 = time.monotonic()
                    cmd, buf = buf, ""
                    resp = slave.handle(cmd)
                    if resp is None:
                        print(f"[sdi12] {cmd!r} -> (silent)", flush=True)
                        continue
                    lat = (time.monotonic() - t0) * 1000.0
                    record(lat)
                    xport.send(resp)
                    # Our own transmission returns on RX; drop it.
                    xport.drain(0.08)
                    buf = ""
                    flag = "" if lat < 15.0 else "   *** OVER 15ms ***"
                    print(f"[sdi12] {cmd!r} -> {resp!r}  "
                          f"{lat:.1f}ms{flag}", flush=True)
                elif len(buf) > 16:
                    buf = ""          # runaway, resynchronise
    except KeyboardInterrupt:
        pass
    finally:
        xport.stop()
        pi.stop()
        if _LAT["n"]:
            print(f"\n[sdi12] final: {_LAT['n']} replies, "
                  f"avg {_LAT['sum']/_LAT['n']:.1f}ms, "
                  f"max {_LAT['max']:.1f}ms, "
                  f"{_LAT['over']} over 15ms", flush=True)


if __name__ == "__main__":
    main()
