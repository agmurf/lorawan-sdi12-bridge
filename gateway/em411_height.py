#!/usr/bin/env python3
"""
River height derivation and validity guards for the Milesight EM411-RDL.

HEIGHT
    height_m = (SENSOR_HEIGHT_MM - distance_mm) / 1000.0

No offset, no scaling, no angle correction -- this is already a real
harbour height, verified against live readings and validated against Fort
Denison at 3.3 cm RMS. Do not "improve" it by adding a datum correction.

THE CONSTANT IS A DEVICE SETTING, NOT A CHOICE HERE
SENSOR_HEIGHT_MM mirrors the Measurement Range configured ON THE
SENSOR. If anyone changes that setting, this constant must change with
it, or every reading silently shifts by the difference -- no error, no
warning, just a gauge that is quietly wrong. That is why it is a named
constant and an environment variable rather than a literal.

WHY THE GUARDS EXIST
SDI-12 has no quality field. A recorder gets a number and believes it.
The server-side pipeline applies these same three guards, so the bridge
must too -- otherwise the radio path reports readings the network path
would have rejected, and the two disagree during exactly the events that
matter.

    blind-zone-status = 1  ->  the value is PINNED AT A FLOOR. It means
                               ">= this height", not a measurement.
    device-tilt = 1        ->  sensor has moved; geometry is no longer
                               the geometry the calibration assumed.
    outside 0.35-1.55 m    ->  below, the radar reads the river bed;
                               above, it enters the blind zone.
                               Mid-band accuracy is ~1.4 cm.

An invalid reading is published as the sentinel, never suppressed
silently: a gauge that stops updating looks identical to a gauge that is
merely quiet, and on a flood network that ambiguity is dangerous.
"""
import os

# Height of the sensor face above the Fort Denison tide datum, in mm.
# This is a SURVEY value held server-side, NOT the device's Measurement
# Range setting (which is 2.0 m and is a different thing entirely).
#
#     height_m = (SENSOR_HEIGHT_MM - distance_mm) / 1000
#
# Verified against four consecutive production readings on
# floodwarning.tech. Changes only if the sensor is physically moved or
# re-mounted, or the datum calibration is redone.
SENSOR_HEIGHT_MM = float(os.environ.get("EM411_SENSOR_HEIGHT_MM", "1946.2"))

# Trustworthy measuring band, metres of river height.
VALID_MIN_M = float(os.environ.get("EM411_MIN_M", "0.35"))
VALID_MAX_M = float(os.environ.get("EM411_MAX_M", "1.55"))

# What we publish when a reading cannot be trusted. Matches the SDI-12
# slave's staleness sentinel so downstream only needs one rule.
#
# -9.999 rather than -9999: the ERT-A2 applies Scale=1000 to this
# channel, so this reaches the radio as -9999 mm -- the conventional
# hydrology no-data value, and small enough for ALERT IFLOWS' integer
# field. -9999.0 would have become -9,999,000 mm and overflowed.
INVALID_SENTINEL = -9.999


def river_height_m(decoded):
    """Validated river height in metres, or the sentinel.

    Returns (value, reason). reason is None when the reading is good, and
    a short human-readable string when the sentinel is being returned --
    so the caller can log WHY rather than just that something failed.
    """
    # Radar reported a fault sentinel rather than a distance.
    if "distance_status" in decoded and "distance_mm" not in decoded:
        return INVALID_SENTINEL, (
            f"radar fault (status {decoded['distance_status']:.0f})")

    if "distance_mm" not in decoded:
        return INVALID_SENTINEL, "no distance in payload"

    # Blind zone: the reported value is a floor, not a measurement. It
    # means ">= this height", which is not something SDI-12 can express.
    if decoded.get("blind_zone_status") == 1.0:
        return INVALID_SENTINEL, "blind-zone alarm: value is a floor, not a reading"

    # Tilt: the sensor has moved, so the geometry behind the calibration
    # no longer holds.
    if decoded.get("tilt") == 1.0:
        return INVALID_SENTINEL, "device tilted: calibration geometry invalid"

    height = (SENSOR_HEIGHT_MM - decoded["distance_mm"]) / 1000.0

    if height < VALID_MIN_M:
        return INVALID_SENTINEL, (
            f"height {height:.3f}m below {VALID_MIN_M}m "
            f"(radar reading the bed)")
    if height > VALID_MAX_M:
        return INVALID_SENTINEL, (
            f"height {height:.3f}m above {VALID_MAX_M}m "
            f"(entering blind zone)")

    return round(height, 3), None
