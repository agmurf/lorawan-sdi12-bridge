#!/bin/sh
# Persist BOTH halves of the chain to a file that survives reboot.
#
# The journal is now persistent too, but this stays: it is a compact,
# greppable history of the two numbers that matter -- reception rate
# (radio in) and SDI-12 health (data out) -- without re-parsing days of
# journal every time.
{
  echo "===== $(date -uIs) ====="
  journalctl -u local-ns-logger --since "-6h" --no-pager | python3 /home/pi/rxstats.py
  echo
  echo "--- SDI-12 ---"
  journalctl -u sdi12-slave --since "-6h" --no-pager | python3 /home/pi/sdi12stats.py
  echo
} >> /home/pi/rxstats.log 2>&1
