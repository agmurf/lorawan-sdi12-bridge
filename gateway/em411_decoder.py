#!/usr/bin/env python3
"""
Milesight EM411-RDL radar level sensor decoder.

Python port of the NNNCo/Actility Parser Studio parser, keeping the same
TLV walk and the same level derivation, so results match what the cloud
platform produces for the same payload.

WHAT IT MEASURES: the radar reports DISTANCE from the sensor face down to
the water surface. River height is the inverse of that:

    level = emptyDistanceMm - (distance + offsetMm)

where emptyDistanceMm is the distance from the sensor face to the datum
(gauge zero / channel invert). So as the river rises, distance falls and
level rises. Getting emptyDistanceMm wrong shifts every reading, so it is
a survey number, not a guess.

UNITS: the sensor and this decoder work in MILLIMETRES. level_metres()
converts to metres because the ERT-A2 applies its own x1000 scaling to
report millimetres on sensor ID 4079.

SENTINELS: 0xFFFD and 0xFFFF are error codes from the radar, not
distances. They are reported as a status channel and never treated as a
reading -- a 65535 mm "distance" would otherwise decode as a large
negative river level.
"""
import os
import struct

# --- per-device geometry -------------------------------------------------
# Distance from the sensor face to the datum (gauge zero), in mm. Required
# for level output; without it only raw distance is available.
EMPTY_DISTANCE_MM = os.environ.get("EM411_EMPTY_DISTANCE_MM")
# Signed trim added to the raw distance before the level calculation
# (mounting correction / dead-band).
OFFSET_MM = float(os.environ.get("EM411_OFFSET_MM", "0"))
# Usable height for percent-full. Defaults to EMPTY_DISTANCE_MM.
TANK_HEIGHT_MM = os.environ.get("EM411_TANK_HEIGHT_MM")

# Housekeeping TLV lengths, channel 0xFF.
HK_LENGTHS = {0x01: 1, 0x09: 2, 0x0A: 2, 0x0B: 1,
              0x0F: 1, 0x16: 8, 0xFE: 1, 0xFF: 2}

DIST_ERROR_SENTINELS = (0xFFFD, 0xFFFF)


def _s16(v):
    """Interpret a 16-bit unsigned value as signed, as the JS parser does."""
    return v - 65536 if v > 32767 else v


def decode_em411(payload, empty_distance_mm=None, offset_mm=None,
                 tank_height_mm=None):
    """Decode an EM411-RDL uplink.

    Returns a dict. 'distance_mm' is always present when the radar gave a
    valid reading; 'level_mm' and 'level_percent' only when geometry is
    supplied. Unknown TLVs stop the walk (same as the reference parser)
    rather than guessing at lengths.
    """
    if empty_distance_mm is None:
        empty_distance_mm = (float(EMPTY_DISTANCE_MM)
                             if EMPTY_DISTANCE_MM else None)
    if offset_mm is None:
        offset_mm = OFFSET_MM
    if tank_height_mm is None:
        tank_height_mm = (float(TANK_HEIGHT_MM) if TANK_HEIGHT_MM
                          else empty_distance_mm)

    out = {}
    i = 0
    n = len(payload)

    def derive_level(dist_mm, key_level="level_mm", key_pct="level_percent"):
        if empty_distance_mm is None:
            return
        level = empty_distance_mm - (dist_mm + offset_mm)
        if level < 0:
            level = 0.0                       # surface cannot be below datum
        if tank_height_mm is not None and level > tank_height_mm:
            level = tank_height_mm
        out[key_level] = float(level)
        if tank_height_mm:
            pct = (level / tank_height_mm) * 100.0
            pct = max(0.0, min(100.0, pct))
            out[key_pct] = round(pct, 2)

    while i + 2 <= n:
        ch = payload[i]
        tp = payload[i + 1]
        i += 2

        # Battery, 1 byte, percent
        if ch == 0x01 and tp == 0x75:
            if i + 1 > n:
                break
            bat = payload[i]
            i += 1
            if bat != 0xFF:
                out["battery_percent"] = float(bat)

        # Distance, 2 bytes LE, mm -- the reading we care about
        elif ch == 0x04 and tp == 0x82:
            if i + 2 > n:
                break
            raw = struct.unpack_from("<H", payload, i)[0]
            i += 2
            if raw in DIST_ERROR_SENTINELS:
                out["distance_status"] = 2.0 if raw == 0xFFFF else 1.0
            else:
                dist = float(_s16(raw))
                out["distance_mm"] = dist
                derive_level(dist)

        # Device position: 0 normal, 1 tilt
        elif ch == 0x05 and tp == 0x00:
            if i + 1 > n:
                break
            out["tilt"] = float(payload[i])
            i += 1

        # Radar signal strength, Int16LE, /100 dBm
        elif ch == 0x06 and tp == 0xC7:
            if i + 2 > n:
                break
            out["radar_dbm"] = struct.unpack_from("<h", payload, i)[0] / 100.0
            i += 2

        # Threshold alarm: distance + status
        elif ch == 0x84 and tp == 0x82:
            if i + 3 > n:
                break
            raw = struct.unpack_from("<H", payload, i)[0]
            status = payload[i + 2]
            i += 3
            adist = float(_s16(raw))
            out["threshold_alarm_distance_mm"] = adist
            out["threshold_alarm_status"] = float(status)
            if raw not in DIST_ERROR_SENTINELS and empty_distance_mm is not None:
                lvl = empty_distance_mm - (adist + offset_mm)
                out["threshold_alarm_level_mm"] = float(max(0.0, lvl))

        # Shift threshold: distance + delta + marker
        elif ch == 0x94 and tp == 0x82:
            if i + 5 > n:
                break
            raw = struct.unpack_from("<H", payload, i)[0]
            shift = struct.unpack_from("<H", payload, i + 2)[0]
            marker = payload[i + 4]
            i += 5
            out["shift_alarm_distance_mm"] = float(_s16(raw))
            out["shift_alarm_delta_mm"] = float(_s16(shift))
            out["shift_alarm_status"] = float(marker)

        # Blind zone alarm
        elif ch == 0xB4 and tp == 0x82:
            if i + 3 > n:
                break
            raw = struct.unpack_from("<H", payload, i)[0]
            status = payload[i + 2]
            i += 3
            if raw not in DIST_ERROR_SENTINELS:
                out["blind_zone_distance_mm"] = float(_s16(raw))
            out["blind_zone_status"] = float(status)

        # Historical data block -- skipped, same as reference parser
        elif ch == 0x20 and tp == 0xCE:
            if i + 11 > n:
                break
            i += 11

        # Query reply codes -- skipped
        elif ch == 0xFC and tp in (0x6B, 0x6C):
            if i + 1 > n:
                break
            i += 1

        # Housekeeping
        elif ch == 0xFF:
            ln = HK_LENGTHS.get(tp)
            if ln is None:
                break
            i += ln

        else:
            break        # unknown TLV: stop rather than mis-parse

    return out


def level_metres(decoded):
    """River level in metres, or None if geometry was not configured.

    Metres because the ERT-A2 applies Scale=1000 to report millimetres.
    """
    if "level_mm" not in decoded:
        return None
    return decoded["level_mm"] / 1000.0
