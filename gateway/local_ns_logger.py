
# ---------------------------------------------------------------------------
# KEY MATERIAL HAS BEEN REMOVED FROM THIS PUBLISHED COPY
#
# The working version of this file on the gateway carries real LoRaWAN keys
# inline. They are stripped here because this repository is public, and an
# AppKey is enough to impersonate the sensor it belongs to.
#
# To run this, keep keys OUT of the source. Put them in device_keys.json
# (chmod 600, gitignored) and load them at start-up:
#
#     import json
#     with open("/home/pi/device_keys.json") as fh:
#         KEYS = json.load(fh)
#     ... bytes.fromhex(KEYS["24E1..."]["AppKey"]) ...
#
# Device identifiers (DevAddr, DevEUI, AppEUI) are placeholders for the
# same reason.
# ---------------------------------------------------------------------------

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Local LoRaWAN UDP "network-server-lite" that only accepts traffic
from a fixed list of ABP devices and writes parsed readings to a serial
SDI-12 interface board. All other packets are dropped.

Strict mode:
- DevAddr must match an entry in DEVICES
- MIC must validate with that device's NwkSKey
- (optional) FPort must be in allowed set for that device
- Only on success do we write to serial

Requires: pycryptodome, pyserial
"""

import base64, json, socket, struct, time, os, threading
from binascii import hexlify
from Crypto.Cipher import AES
from em411_decoder import decode_em411
from em411_height import (river_height_m, INVALID_SENTINEL,
                          SENSOR_HEIGHT_MM, VALID_MIN_M, VALID_MAX_M)
import otaa
from Crypto.Hash import CMAC

# -----------------------
# CONFIG
# -----------------------
BIND_HOST = "0.0.0.0"
BIND_PORT = 1700

# Readings are published to a JSON state file that sdi12_run.py serves to
# the ERT-A2. This decouples the LoRa decode path from the SDI-12 timing
# path entirely -- neither can stall the other.
STATE_FILE = os.environ.get("SDI12_STATE", "/home/pi/river_state.json")

# Which decoded field carries the river level, in METRES. The ERT-A2
# applies x1000 scaling of its own to report millimetres on sensor 4079.
# The provisioned device is currently a RisingHF temp/humidity unit, so
# this is a stand-in until the real river sensor is deployed.
RIVER_FIELD = os.environ.get("RIVER_FIELD", "temperature")

# Your allowed ABP devices (DevAddr is hex, big-endian as typically shown)
# FPort_allowed: None to allow any, or set like {8}
DEVICES = {
    # RisingHF temperature/humidity test unit (the original device).
    "<<DEVADDR>>": {
        "NwkSKey": bytes.fromhex(KEYS["<<REDACTED>>"]),
        "AppSKey": bytes.fromhex(KEYS["<<REDACTED>>"]),
        "FPort_allowed": {8},
        "decoder": "risinghf",
        # Optional rolling FCnt32 tracking per device (persist not required)
        "fcnt32": 0
    },

    # --- Milesight EM411-RDL radar river level sensor -------------------
    # Fill in the DevAddr (as the dict key), NwkSKey and AppSKey from the
    # device's ABP provisioning, then remove the leading "#".
    #
    # NOTE ON FPort: Milesight devices commonly uplink on FPort 85, not 8.
    # Confirm against a real uplink -- the wrong value here silently drops
    # every packet at the FPort check, which looks exactly like no
    # reception at all.
    #
    # NOTE ON JOIN MODE: this is a minimal ABP-only network server. An
    # OTAA device will never appear, because join requests are not handled.
    # The EM411 must be provisioned ABP.
    #
    # "AABBCCDD": {
    #     "NwkSKey": bytes.fromhex("<32 hex chars>"),
    #     "AppSKey": bytes.fromhex("<32 hex chars>"),
    #     "FPort_allowed": {85},
    #     "decoder": "em411",
    #     "fcnt32": 0
    # },
}

# --- OTAA devices --------------------------------------------------
# Keyed by DevEUI (big-endian, as printed on the device). These have no
# session until they join; one is minted on each successful JoinRequest.
OTAA_DEVICES = {
    "<<DEVEUI>>": {                       # Milesight EM411-RDL
        "AppKey": bytes.fromhex(KEYS["<<REDACTED>>"]),
        "AppEUI": "<<APPEUI>>",
        "FPort_allowed": None,                  # accept any; Milesight uses 85
        "decoder": "em411",
    },
}

# Answering a JoinRequest HIJACKS the device onto this network: it
# re-keys to our DevAddr and stops reporting to whichever network it was
# joined to. Only enable this for a device that is OURS exclusively.
#
# For a sensor already in production elsewhere, leave this off and use
# PASSIVE INTERCEPTION instead -- copy the active DevAddr/NwkSKey/AppSKey
# out of that network's console into DEVICES above. We hear every uplink
# regardless; the session keys are the only thing we lack.
# "0" never | "1" always | "auto" only after the device shows nobody
# else is answering. See module notes in patch_failover.py.
ANSWER_JOINS_MODE = os.environ.get("OTAA_ANSWER_JOINS", "0").lower()
ANSWER_JOINS = ANSWER_JOINS_MODE == "1"

# Failover thresholds for "auto".
JOIN_FAILOVER_COUNT = int(os.environ.get("JOIN_FAILOVER_COUNT", "3"))
JOIN_FAILOVER_WINDOW = int(os.environ.get("JOIN_FAILOVER_WINDOW", "1800"))

# Sessions survive restarts. Without this, a restart would orphan the
# device: it keeps uplinking with keys we no longer hold, every frame
# failing its MIC, and only power-cycling the SENSOR recovers it.
SESSION_FILE = os.environ.get("LORAWAN_SESSIONS",
                              "/home/pi/lorawan_sessions.json")

# Externally-managed session keys, e.g. copied from ThingPark/N2N for a
# device that is joined to another network. Optional -- absent means the
# hardcoded DEVICES entries stand. Secrets live here, not in source.
DEVICE_KEYS_FILE = os.environ.get("DEVICE_KEYS",
                                  "/home/pi/device_keys.json")

# Where downlinks go. The packet forwarder announces itself with
# PULL_DATA; JoinAccepts are sent back to that same socket as PULL_RESP.
PULL_ADDR = None

# If you want to also parse and forward battery, set to True and provide your parser
FORWARD_PARSED = True

def now():
    return time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())


# -----------------------
# OTAA session store
# -----------------------
def load_device_keys():
    """Merge externally-supplied session keys into DEVICES.

    Validates before trusting: a 16-byte key that is actually 15 bytes,
    or hex with a stray space, would otherwise fail every MIC silently
    and look exactly like the device not transmitting.
    """
    try:
        with open(DEVICE_KEYS_FILE) as fh:
            raw = json.load(fh)
    except FileNotFoundError:
        return 0
    except (OSError, ValueError) as e:
        print(f"[{now()}] ERROR: {DEVICE_KEYS_FILE} unreadable: {e}",
              flush=True)
        return 0

    n = 0
    for devaddr, s in raw.items():
        try:
            addr = devaddr.strip().upper()
            if len(addr) != 8 or not all(c in "0123456789ABCDEF" for c in addr):
                raise ValueError(f"DevAddr {devaddr!r} is not 8 hex chars")
            nwks = bytes.fromhex(s["NwkSKey"].strip())
            apps = bytes.fromhex(s["AppSKey"].strip())
            if len(nwks) != 16 or len(apps) != 16:
                raise ValueError(
                    f"keys must be 16 bytes, got {len(nwks)}/{len(apps)}")
            DEVICES[addr] = {
                "NwkSKey": nwks,
                "AppSKey": apps,
                "FPort_allowed": (set(s["FPort_allowed"])
                                  if s.get("FPort_allowed") else None),
                "decoder": s.get("decoder", "em411"),
                "external": True,      # do not overwrite via save_sessions()
                "fcnt32": 0,
            }
            fports = DEVICES[addr]["FPort_allowed"]
            fport_txt = (",".join(str(x) for x in sorted(fports))
                         if fports else "any")
            msg = (f"[{now()}] loaded external keys for DevAddr {addr} "
                   f"(decoder {DEVICES[addr]['decoder']}, "
                   f"fport {fport_txt})")
            note = s.get("note", "")
            if note:
                msg += "  -- " + note
            print(msg, flush=True)
            n += 1
        except (KeyError, ValueError, AttributeError) as e:
            print(f"[{now()}] ERROR: bad entry for {devaddr!r}: {e}",
                  flush=True)
    return n


def load_sessions():
    """Restore joined devices into DEVICES so uplinks keep validating."""
    try:
        with open(SESSION_FILE) as fh:
            saved = json.load(fh)
    except (OSError, ValueError):
        return 0
    n = 0
    for devaddr, s in saved.items():
        try:
            DEVICES[devaddr] = {
                "NwkSKey": bytes.fromhex(s["NwkSKey"]),
                "AppSKey": bytes.fromhex(s["AppSKey"]),
                "FPort_allowed": (set(s["FPort_allowed"])
                                  if s.get("FPort_allowed") else None),
                "decoder": s.get("decoder", "em411"),
                "deveui": s.get("deveui"),
                "fcnt32": int(s.get("fcnt32", 0)),
            }
            n += 1
        except (KeyError, ValueError):
            continue
    return n


def save_sessions():
    """Persist every OTAA-derived session (those carrying a deveui)."""
    out = {}
    for devaddr, d in DEVICES.items():
        if d.get("external"):
            continue        # externally managed; not ours to persist
        if not d.get("deveui"):
            continue
        out[devaddr] = {
            "deveui": d["deveui"],
            "NwkSKey": d["NwkSKey"].hex(),
            "AppSKey": d["AppSKey"].hex(),
            "FPort_allowed": (sorted(d["FPort_allowed"])
                              if d.get("FPort_allowed") else None),
            "decoder": d.get("decoder", "em411"),
            "fcnt32": d.get("fcnt32", 0),
        }
    tmp = SESSION_FILE + ".tmp"
    try:
        with open(tmp, "w") as fh:
            json.dump(out, fh, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, SESSION_FILE)
    except OSError as e:
        print(f"[{now()}] ERROR: cannot save sessions: {e}", flush=True)


_seen_devnonces = {}

# When each JoinRequest was seen, per DevEUI. Repeated joins that nobody
# answers are the clearest evidence the primary network has died.
_join_history = {}

# Last time we logged each unseen DevAddr, so a busy band cannot flood the
# journal while a genuinely new device is still reported immediately.
_unknown_seen = {}

# Rate limiter for weak-frame logging (list so it is mutable in scope).
_last_crc_log = [0.0]
_last_mic_log = [0.0]
_last_fport_log = [0.0]


def handle_join_request(phy, rx, sock):
    """Validate a JoinRequest, mint a session, and send the JoinAccept.

    Returns True if a JoinAccept was transmitted.
    """
    global PULL_ADDR
    try:
        req = otaa.parse_join_request(phy)
    except ValueError as e:
        print(f"[{now()}] join: malformed request ({e})", flush=True)
        return False

    dev = OTAA_DEVICES.get(req["deveui"])
    if dev is None:
        print(f"[{now()}] join: unknown DevEUI {req['deveui']}, ignored",
              flush=True)
        return False

    if not otaa.join_request_mic_ok(dev["AppKey"], req):
        print(f"[{now()}] join: BAD MIC from {req['deveui']} -- wrong AppKey?",
              flush=True)
        return False

    # We validated the request, so this really is our device re-keying.
    # Record it: repeated joins nobody answers are how we learn the
    # primary network has gone away.
    t = time.time()
    hist = _join_history.setdefault(req["deveui"], [])
    hist.append(t)
    del hist[:-32]
    recent = [x for x in hist if t - x <= JOIN_FAILOVER_WINDOW]

    answer = ANSWER_JOINS
    if ANSWER_JOINS_MODE == "auto" and len(recent) >= JOIN_FAILOVER_COUNT:
        answer = True
        print(f"[{now()}] FAILOVER: {len(recent)} unanswered joins from "
              f"{req['deveui']} in {JOIN_FAILOVER_WINDOW}s -- the primary "
              f"network is not responding. Taking over the session so the "
              f"redundant path keeps reporting.", flush=True)

    if not answer:
        remaining = ""
        if ANSWER_JOINS_MODE == "auto":
            remaining = (f" ({len(recent)}/{JOIN_FAILOVER_COUNT} toward "
                         f"failover)")
        print(f"[{now()}] JOIN SEEN from {req['deveui']} (DevNonce "
              f"{req['devnonce'].hex()}) -- not answering{remaining}.",
              flush=True)
        print(f"[{now()}]   Device is re-keying. Session keys copied from "
              f"the primary network are now STALE; uplinks will stop "
              f"decoding until they are refreshed.", flush=True)
        return False

    # DevNonce replay check. The spec requires rejecting reuse; a repeat
    # is either a replay or a device that reset its nonce counter.
    nonce = req["devnonce"].hex()
    seen = _seen_devnonces.setdefault(req["deveui"], [])
    if nonce in seen:
        print(f"[{now()}] join: repeated DevNonce {nonce} from "
              f"{req['deveui']}, ignored", flush=True)
        return False
    seen.append(nonce)
    del seen[:-64]

    if PULL_ADDR is None:
        print(f"[{now()}] join: no PULL_DATA seen yet, cannot send "
              f"JoinAccept -- device will retry", flush=True)
        return False

    # Mint the session.
    appnonce = os.urandom(3)
    netid = otaa.DEFAULT_NETID
    counter = 1 + sum(1 for d in DEVICES.values() if d.get("deveui"))
    devaddr_b = otaa.next_devaddr(netid, counter)
    devaddr = devaddr_b.hex().upper()
    nwkskey, appskey = otaa.derive_session_keys(
        dev["AppKey"], appnonce, netid, req["devnonce"])

    DEVICES[devaddr] = {
        "NwkSKey": nwkskey,
        "AppSKey": appskey,
        "FPort_allowed": dev.get("FPort_allowed"),
        "decoder": dev.get("decoder", "em411"),
        "deveui": req["deveui"],
        "fcnt32": 0,
    }
    save_sessions()

    ja = otaa.build_join_accept(dev["AppKey"], appnonce, netid, devaddr_b)
    b64 = base64.b64encode(ja).decode()
    msg = otaa.build_txpk(rx, b64)
    msg["txpk"]["data"] = b64
    msg["txpk"]["size"] = len(ja)

    # PULL_RESP: version, token, 0x03, JSON
    pkt = bytes([0x02, 0x00, 0x00, 0x03]) + json.dumps(msg).encode()
    try:
        sock.sendto(pkt, PULL_ADDR)
    except OSError as e:
        print(f"[{now()}] join: could not send JoinAccept: {e}", flush=True)
        return False

    print(f"[{now()}] JOIN OK: {req['deveui']} -> DevAddr {devaddr} "
          f"(JoinAccept queued for {rx['freq']}MHz {rx['datr']}, "
          f"RX1 +{otaa.JOIN_ACCEPT_DELAY1}s)", flush=True)
    return True

# -----------------------
# LoRaWAN helpers (ABP uplink)
# -----------------------
def aes128_encrypt(key, block):
    cipher = AES.new(key, AES.MODE_ECB)
    return cipher.encrypt(block)

def lorawan_mic_ok(nwkskey, mhdr_macpayload, mic_given, devaddr_le, fcnt, direction=0):
    # Calculate MIC per LoRaWAN spec 1.0.x using CMAC with B0 block
    # direction: 0 uplink, 1 downlink
    L = len(mhdr_macpayload)
    b0 = bytes([0x49, 0,0,0,0,  direction]) + devaddr_le + struct.pack("<I", fcnt) + bytes([0, L])
    cobj = CMAC.new(nwkskey, ciphermod=AES)
    cobj.update(b0 + mhdr_macpayload)
    mic_calc = cobj.digest()[:4]
    return mic_calc == mic_given

def lorawan_decrypt_appskey(appskey, devaddr_le, fcnt, frm_payload):
    # LoRaWAN payload encryption uses AES-128 CTR-like with A_i blocks
    if not frm_payload:
        return b""
    out = bytearray()
    k = 1
    for i in range(0, len(frm_payload), 16):
        block = bytes([0x01, 0,0,0,0, 0]) + devaddr_le + struct.pack("<I", fcnt) + bytes([0, k])
        s = aes128_encrypt(appskey, block)
        chunk = frm_payload[i:i+16]
        out.extend(x ^ y for x, y in zip(chunk, s[:len(chunk)]))
        k += 1
    return bytes(out)

def parse_phypayload(pkt_b):
    # Minimal LoRaWAN 1.0.x uplink parser
    # Returns dict with keys: mtype, devaddr_be, devaddr_le, fcnt16, fport, frm_payload, mic, mhdr_macpayload
    if len(pkt_b) < 12:
        raise ValueError("Too short for PHYPayload")
    mhdr = pkt_b[0]
    mtype = (mhdr >> 5) & 0x07  # 0=JoinReq, 2=UnconfUp, 4=ConfUp, etc.
    mic = pkt_b[-4:]
    macpayload = pkt_b[1:-4]
    # FHDR
    devaddr_le = macpayload[0:4]
    devaddr_be = devaddr_le[::-1].hex().upper()
    fctrl = macpayload[4]
    fcnt16 = struct.unpack("<H", macpayload[5:7])[0]
    # FCtrl bits, uplink: 7=ADR 6=ADRACKReq 5=ACK 4=ClassB 3..0=FOptsLen.
    # ADRACKReq is the device asking "is anyone there?" after ~64 uplinks
    # with no downlink -- a direct report that its network is not
    # answering, which is exactly what a redundant path wants to know.
    adr = bool(fctrl & 0x80)
    adr_ack_req = bool(fctrl & 0x40)
    ack = bool(fctrl & 0x20)
    # FOpts length
    fopts_len = fctrl & 0x0F
    pos = 7 + fopts_len
    if pos > len(macpayload):
        raise ValueError("Bad FOpts length")
    fport = macpayload[pos] if pos < len(macpayload) else None
    frm_payload = macpayload[pos+1:] if fport is not None else b""
    return {
        "mtype": mtype,
        "devaddr_be": devaddr_be,
        "devaddr_le": devaddr_le,
        "fcnt16": fcnt16,
        "adr": adr,
        "adr_ack_req": adr_ack_req,
        "ack": ack,
        "fport": fport,
        "frm_payload": frm_payload,
        "mic": mic,
        "mhdr_macpayload": pkt_b[:-4],  # MHDR|MACPayload (without MIC)
    }

def next_fcnt32(base32, fcnt16):
    """
    Extend a 16-bit FCnt to 32 bits.

    Only treat a decrease as a rollover if it is a LARGE one. A small decrease
    is a retransmit or an out-of-order frame; counting that as a rollover adds
    65536 to the counter and every later MIC check fails forever.
    """
    base_low = base32 & 0xFFFF
    candidate = (base32 & ~0xFFFF) | fcnt16
    if fcnt16 < base_low and (base_low - fcnt16) > 0x8000:
        candidate += 0x10000
    return candidate

# -----------------------
# Publishing readings
# -----------------------
_last_write_warn = 0.0

def publish(values):
    """Write readings for the SDI-12 slave to serve.

    Written to a temp file and renamed, because the slave may read this
    at any instant -- including mid-poll from the ERT-A2. Rename is
    atomic within a filesystem; a plain write is not, and a half-written
    file would surface as a bogus river level.
    """
    global _last_write_warn
    tmp = STATE_FILE + ".tmp"
    try:
        with open(tmp, "w") as fh:
            json.dump({"values": values, "updated": time.time()}, fh)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, STATE_FILE)
        return True
    except OSError as e:
        t = time.time()
        if t - _last_write_warn > 60:
            print(f"[{now()}] ERROR: cannot write {STATE_FILE}: {e}",
                  flush=True)
            _last_write_warn = t
        return False

# (the old write_csv/ensure_serial pair drove an Arduino over
# /dev/ttySDI12; that board is retired and the path is now the state file)

# -----------------------
# Optional payload decoding
# (Your RisingHF parser simplified  adapt if needed)
# -----------------------

def decode_risinghf(payload: bytes):
    """
    RisingHF 9-byte payload:
      [0] status (0x81/0x01)
      [1:3] int16 LE: temperature_raw -> T = raw*175.72/65536 - 46.85
      [3]   uint8   : humidity_raw   -> RH = raw*125/256 - 6
      [4:6] int16 LE: period_raw     -> seconds = raw*2
      [6]   uint8   : lora_rssi      -> rssi = raw - 180
      [7]   uint8   : lora_snr       -> snr  = raw/4
      [8]   uint8   : battery_raw    -> Vbat = (raw+150)/100
    """
    import struct
    if len(payload) < 9:
        return None

    b = payload
    status = b[0]
    temp_raw = struct.unpack('<h', b[1:3])[0]
    hum_raw  = b[3]
    per_raw  = struct.unpack('<h', b[4:6])[0]
    rssi     = b[6] - 180
    snr      = b[7] / 4
    vbat     = (b[8] + 150) / 100.0

    temperature = temp_raw * 175.72 / 65536.0 - 46.85
    humidity    = hum_raw  * 125.0   / 256.0   - 6.0
    period_s    = per_raw * 2

    return {
        "status": f"0x{status:02X}",
        "temperature": round(temperature, 2),
        "humidity": round(humidity, 2),
        "period": period_s,
        "lora_rssi": float(rssi),
        "lora_snr": float(round(snr, 2)),
        "battery": round(vbat, 2),
    }

def run_server():
    global PULL_ADDR
    restored = load_sessions()
    if restored:
        print(f"Restored {restored} OTAA session(s) from {SESSION_FILE}",
              flush=True)
    external = load_device_keys()
    if external:
        print(f"Loaded {external} external session key set(s) from "
              f"{DEVICE_KEYS_FILE}", flush=True)
    else:
        print(f"No external keys ({DEVICE_KEYS_FILE} absent or empty) -- "
              f"uplinks from unprovisioned DevAddrs will be logged but "
              f"not decoded", flush=True)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind((BIND_HOST, BIND_PORT))
    print("Local LoRaWAN logger starting (strict mode)...", flush=True)
    print("Listening on %s:%d" % (BIND_HOST, BIND_PORT), flush=True)
    print(f"Publishing to {STATE_FILE} (metres)", flush=True)
    print(f"  EM411 height = ({SENSOR_HEIGHT_MM:.1f}mm - distance) / 1000"
          f", valid band {VALID_MIN_M}-{VALID_MAX_M}m"
          f", invalid -> {INVALID_SENTINEL}", flush=True)
    if OTAA_DEVICES:
        if ANSWER_JOINS_MODE == "1":
            mode = "ANSWERING joins (this network owns the device)"
        elif ANSWER_JOINS_MODE == "auto":
            mode = (f"AUTO failover -- passive until {JOIN_FAILOVER_COUNT} "
                    f"unanswered joins within {JOIN_FAILOVER_WINDOW}s, "
                    f"then take over")
        else:
            mode = ("PASSIVE, never answering joins (device belongs to "
                    "another network; needs its session keys copied in)")
        print("  OTAA devices: " + ", ".join(sorted(OTAA_DEVICES)), flush=True)
        print(f"  join policy : {mode}", flush=True)
    abp = sorted(k for k, v in DEVICES.items() if not v.get("deveui"))
    if abp:
        parts = []
        for k in abp:
            parts.append(k + " (" + DEVICES[k].get("decoder", "risinghf") + ")")
        print("  ABP devices : " + ", ".join(parts), flush=True)

    drops = 0
    accepts = 0
    crc_fail = 0
    recovered = 0          # passed the MIC despite a failed CRC
    delivered = 0
    undelivered = 0
    last_stat = time.time()

    while True:
        data, addr = sock.recvfrom(65535)
        if len(data) < 4:
            continue

        proto = data[0]
        token = data[1:3]
        ptype = data[3]

        # PULL_DATA tells us where downlinks must be sent. Without this
        # address a JoinAccept has nowhere to go.
        if ptype == 0x02:
            if PULL_ADDR != addr:
                PULL_ADDR = addr
                print(f"[{now()}] downlink path: {addr[0]}:{addr[1]}",
                      flush=True)

        # Acknowledge the packet forwarder. Semtech UDP expects PUSH_ACK (0x01)
        # for PUSH_DATA (0x00) and PULL_ACK (0x04) for PULL_DATA (0x02).
        # Without these the forwarder reports 0% acknowledged and cannot tell a
        # live server from a dead one.
        try:
            if ptype == 0x00:
                sock.sendto(bytes([proto]) + token + bytes([0x01]), addr)
            elif ptype == 0x02:
                sock.sendto(bytes([proto]) + token + bytes([0x04]), addr)
        except Exception as e:
            print(f"[{now()}] WARNING: ack send failed: {e}", flush=True)

        # 0x00 PUSH_DATA (uplink), 0x02 PULL_DATA (keepalive)
        if ptype == 0x00:
            # header(4) + token(2) + gwid(8) + json
            try:
                js = json.loads(data[12:].decode("utf-8", "ignore"))
            except Exception:
                drops += 1
                continue

            rxpks = js.get("rxpk", [])
            for rx in rxpks:
                # stat: 1 = CRC OK, -1 = CRC failed, 0 = no CRC.
                #
                # A CRC failure is a HINT, not a verdict. It used to end
                # processing here, which put the trust boundary on the
                # weaker of the two checks we have: the concentrator's
                # CRC is 16 bits, while the LoRaWAN MIC is a 32-bit
                # CMAC-AES keyed with the NwkSKey. Nothing corrupt or
                # forged passes the MIC (2^-32), so rejecting on the CRC
                # discarded every spuriously-flagged frame without ever
                # offering it to the check that could vindicate it.
                #
                # The frame now goes through the normal path and the MIC
                # decides. Unauthenticated frames are still dropped.
                crc_failed = rx.get("stat") == -1
                if crc_failed:
                    crc_fail += 1
                    t = time.time()
                    if t - _last_crc_log[0] > 60:
                        _last_crc_log[0] = t
                        print(f"[{now()}] CRC-failed frame: "
                              f"rssi={rx.get('rssi')}dBm "
                              f"snr={rx.get('lsnr')}dB "
                              f"{rx.get('datr')} {rx.get('freq')}MHz "
                              f"size={rx.get('size')}B "
                              f"-- passing to MIC for adjudication "
                              f"(total this run: {crc_fail})", flush=True)

                try:
                    payload = base64.b64decode(rx["data"])
                except Exception:
                    drops += 1
                    continue

                # JoinRequest (MType 0) needs the OTAA path, not the
                # ABP uplink parser -- it has no DevAddr or FCnt at all.
                if payload and (payload[0] >> 5) == 0:
                    if handle_join_request(payload, rx, sock):
                        accepts += 1
                    else:
                        drops += 1
                    continue

                # Parse LoRaWAN PHYPayload
                try:
                    p = parse_phypayload(payload)
                except Exception:
                    drops += 1
                    continue

                dev_be = p["devaddr_be"]
                dev = DEVICES.get(dev_be)
                if not dev and crc_failed:
                    # No keys for this address AND the CRC failed, so the
                    # DevAddr may itself be corruption. Nothing here can
                    # be verified; drop it without logging an address
                    # that probably does not exist.
                    drops += 1
                    continue

                if not dev:
                    # Not a DevAddr we hold keys for. Log it anyway: this
                    # is how an un-provisioned device (the EM411, before
                    # its session keys are copied across) becomes visible.
                    t = time.time()
                    first = dev_be not in _unknown_seen
                    if first or t - _unknown_seen[dev_be] > 60:
                        _unknown_seen[dev_be] = t
                        print(f"[{now()}] UNKNOWN DevAddr {dev_be}"
                              f"{' (FIRST SIGHT)' if first else ''}"
                              f"  fport={p.get('fport')}"
                              f" fcnt={p.get('fcnt16')}"
                              f" bytes={len(p.get('frm_payload') or b'')}"
                              f" rssi={rx.get('rssi')}dBm"
                              f" snr={rx.get('lsnr')}dB"
                              f" {rx.get('datr')} {rx.get('freq')}MHz"
                              f"  -- no session keys held, cannot decode",
                              flush=True)
                    drops += 1
                    continue

                # FCnt32 reconstruction (rollover extension)
                new32 = next_fcnt32(dev.get("fcnt32", 0), p["fcnt16"])

                # MIC check with NwkSKey
                if not lorawan_mic_ok(dev["NwkSKey"], p["mhdr_macpayload"], p["mic"], p["devaddr_le"], new32, direction=0):
                    # This was silent, and that was a real gap. A wrong
                    # NwkSKey or a mis-reconstructed FCnt looks EXACTLY
                    # like poor radio reception: the frame arrives, the
                    # counter never advances, and nothing in the log
                    # says why. Rate limited so a persistent mismatch
                    # cannot flood the journal.
                    t = time.time()
                    if not crc_failed and t - _last_mic_log[0] > 30:
                        _last_mic_log[0] = t
                        print(f"[{now()}] MIC FAIL {dev_be}: "
                              f"fcnt16={p['fcnt16']} -> fcnt32={new32}, "
                              f"fport={p.get('fport')}, "
                              f"rssi={rx.get('rssi')}dBm {rx.get('datr')} "
                              f"-- wrong NwkSKey or FCnt desync. The radio "
                              f"heard this frame; we rejected it.",
                              flush=True)
                    drops += 1
                    continue

                # The MIC has now vouched for this frame, so if the CRC
                # said otherwise the CRC was wrong. Count it: this number
                # is the entire justification for the change, and if it
                # stays at zero the old behaviour cost us nothing.
                if crc_failed:
                    recovered += 1
                    print(f"[{now()}] RECOVERED {dev_be}: failed the "
                          f"concentrator CRC, PASSED the LoRaWAN MIC "
                          f"(fcnt={new32}, rssi={rx.get('rssi')}dBm "
                          f"snr={rx.get('lsnr')}dB {rx.get('datr')}) "
                          f"-- authentic, would have been discarded",
                          flush=True)

                # FPort check
                fpa = dev.get("FPort_allowed")
                if fpa is not None and p["fport"] not in fpa:
                    # Also previously silent. A device that starts using
                    # a second port (MAC traffic on fport 0, or a new
                    # payload type) would simply stop being reported,
                    # with the loss indistinguishable from bad coverage.
                    t = time.time()
                    if t - _last_fport_log[0] > 30:
                        _last_fport_log[0] = t
                        print(f"[{now()}] FPORT REJECT {dev_be}: "
                              f"fport={p['fport']} not in {sorted(fpa)} "
                              f"-- decoded fine, discarded by policy",
                              flush=True)
                    drops += 1
                    continue

                # The device is telling us its network has stopped
                # answering. It keeps uplinking (and steps its data rate
                # down, which our multi-SF concentrator still hears), so
                # this is informational -- but on a redundant path it is
                # the first sign the primary has gone.
                if p.get("adr_ack_req"):
                    print(f"[{now()}] {dev_be}: ADRACKReq set -- device has "
                          f"had no downlink for ~64 uplinks. Primary network "
                          f"may be down; we are still receiving.", flush=True)

                # Only commit the counter once the frame is fully validated, so a
                # forged or corrupt frame cannot advance it.
                dev["fcnt32"] = new32
                if dev.get("deveui") and new32 % 20 == 0:
                    save_sessions()      # checkpoint, not every frame

                # Decrypt FRMPayload with AppSKey
                app_clear = lorawan_decrypt_appskey(dev["AppSKey"], p["devaddr_le"], new32, p["frm_payload"])

                # Decode and forward to the SDI-12 board (CSV) if parser is enabled
                if FORWARD_PARSED:
                    which = dev.get("decoder", "risinghf")

                    if which == "em411":
                        d = decode_em411(app_clear)
                        level, why = river_height_m(d)
                        if why:
                            # Publish the sentinel rather than drop: a stale
                            # last-good value left in place would be
                            # retransmitted as though it were current.
                            print(f"[{now()}] EM411 {dev_be}: INVALID -- {why}"
                                  f"  [dist={d.get('distance_mm')}mm "
                                  f"tilt={d.get('tilt')} "
                                  f"blindzone={d.get('blind_zone_status')}] "
                                  f"-> publishing {INVALID_SENTINEL} "
                                  # Same metadata suffix as the accepted
                                  # line. Without the fcnt here, a frame we
                                  # RECEIVED but rejected was invisible to
                                  # rxstats.py, which undercounted reception
                                  # by exactly the frames our own band was
                                  # throwing away -- the measurement hid the
                                  # very bug it should have exposed.
                                  f"fcnt={new32} adr={p['adr']} "
                                  f"[rx {rx.get('rssi')}dBm "
                                  f"{rx.get('lsnr')}dB {rx.get('datr')} "
                                  f"{rx.get('freq')}MHz]",
                                  flush=True)
                        else:
                            print(f"[{now()}] EM411 {dev_be}: "
                                  f"distance={d.get('distance_mm')}mm "
                                  f"height={level}m "
                                  f"batt={d.get('battery_percent')}% "
                                  f"radar={d.get('radar_dbm')}dBm fcnt={new32} adr={p['adr']} [rx {rx.get('rssi')}dBm {rx.get('lsnr')}dB {rx.get('datr')} {rx.get('freq')}MHz]", flush=True)
                    else:
                        d = decode_risinghf(app_clear)
                        if d is None:
                            print(f"[{now()}] WARNING: undecodable payload from "
                                  f"{dev_be} ({len(app_clear)} bytes), skipped",
                                  flush=True)
                            drops += 1
                            continue
                        level = d.get(RIVER_FIELD)
                        if level is None:
                            print(f"[{now()}] WARNING: decoded payload has no "
                                  f"'{RIVER_FIELD}' field; nothing published",
                                  flush=True)
                            drops += 1
                            continue
                    if publish([float(level)]):
                        delivered += 1
                        print(f"[{now()}] published {float(level):.3f} m "
                              f"from {dev_be} ({which})", flush=True)
                    else:
                        undelivered += 1
                accepts += 1

        # Occasionally print a terse heartbeat
        t = time.time()
        if t - last_stat > 60:
            print(f"[{now()}] accept={accepts} drop={drops} "
                  f"published={delivered} failed={undelivered} "
                  f"crc_fail={crc_fail} recovered={recovered}", flush=True)
            accepts = 0
            drops = 0
            crc_fail = 0
            recovered = 0
            delivered = 0
            undelivered = 0
            last_stat = t

def main():
    run_server()

if __name__ == "__main__":
    main()
