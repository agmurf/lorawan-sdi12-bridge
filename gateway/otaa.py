#!/usr/bin/env python3
"""
LoRaWAN 1.0.x OTAA join support.

The existing network-server-lite was ABP-only: it assumed session keys
were known in advance. An OTAA device has none until it joins, so this
adds the missing half -- parse the JoinRequest, validate it, mint a
session, and build the JoinAccept the device needs to derive the same
keys we do.

TWO THINGS MAKE THIS HARDER THAN THE UPLINK PATH:

1. JoinAccept is ENCRYPTED WITH AES-DECRYPT. That is not a typo in the
   spec: the server runs the decrypt primitive so the device can recover
   it with a single encrypt, which is cheaper on a constrained radio.
   Using encrypt here produces a frame the device silently ignores.

2. It requires a DOWNLINK. The gateway must transmit at a precise time --
   JOIN_ACCEPT_DELAY1 is 5 seconds after the join request, on the same
   channel. Miss the window and the device hears nothing and retries.

BYTE ORDER: EUIs are transmitted little-endian, but everyone writes them
big-endian. Config here holds them the human way and reverses on the
wire, because getting this backwards produces a MIC failure that looks
exactly like a wrong AppKey.
"""
import os
import struct

from Crypto.Cipher import AES
from Crypto.Hash import CMAC

# JOIN_ACCEPT_DELAY1, seconds (LoRaWAN 1.0.x). RX2 would be +6s.
JOIN_ACCEPT_DELAY1 = 5

MHDR_JOIN_REQUEST = 0x00
MHDR_JOIN_ACCEPT = 0x20

# Private/experimental NetID. 0x000000 and 0x000001 are reserved for
# experimental use, which is exactly what a private gateway is.
DEFAULT_NETID = bytes.fromhex("000000")


def _cmac(key, data):
    c = CMAC.new(key, ciphermod=AES)
    c.update(data)
    return c.digest()[:4]


def parse_join_request(phy):
    """Parse and structurally validate a JoinRequest.

    Returns dict with appeui/deveui (big-endian hex, as humans write
    them), devnonce, and the bytes the MIC covers. Raises ValueError on
    anything malformed.
    """
    if len(phy) != 23:
        raise ValueError(f"JoinRequest must be 23 bytes, got {len(phy)}")
    if phy[0] != MHDR_JOIN_REQUEST:
        raise ValueError(f"not a JoinRequest (MHDR 0x{phy[0]:02X})")
    appeui_le = phy[1:9]
    deveui_le = phy[9:17]
    devnonce = phy[17:19]
    mic = phy[19:23]
    return {
        "appeui": appeui_le[::-1].hex().upper(),
        "deveui": deveui_le[::-1].hex().upper(),
        "devnonce": devnonce,
        "mic": mic,
        "mic_data": phy[:19],
    }


def join_request_mic_ok(appkey, req):
    return _cmac(appkey, req["mic_data"]) == req["mic"]


def derive_session_keys(appkey, appnonce, netid, devnonce):
    """NwkSKey and AppSKey per LoRaWAN 1.0.x.

        NwkSKey = aes128_encrypt(AppKey, 0x01 | AppNonce | NetID | DevNonce | pad)
        AppSKey = aes128_encrypt(AppKey, 0x02 | AppNonce | NetID | DevNonce | pad)

    All fields little-endian as transmitted, zero-padded to 16 bytes.
    """
    cipher = AES.new(appkey, AES.MODE_ECB)

    def block(prefix):
        b = bytes([prefix]) + appnonce + netid + devnonce
        return b + b"\x00" * (16 - len(b))

    return cipher.encrypt(block(0x01)), cipher.encrypt(block(0x02))


def build_join_accept(appkey, appnonce, netid, devaddr, dlsettings=0x00,
                      rxdelay=0x01, cflist=b""):
    """Build the encrypted JoinAccept PHYPayload.

    devaddr is big-endian bytes (as displayed); it goes on the wire
    little-endian.
    """
    body = (appnonce + netid + devaddr[::-1]
            + bytes([dlsettings, rxdelay]) + cflist)
    mic = _cmac(appkey, bytes([MHDR_JOIN_ACCEPT]) + body)
    # AES-DECRYPT, deliberately -- see module docstring.
    enc = AES.new(appkey, AES.MODE_ECB).decrypt(body + mic)
    return bytes([MHDR_JOIN_ACCEPT]) + enc


def decrypt_join_accept(appkey, phy):
    """Recover a JoinAccept the way the device does. Used by tests.

    Returns (fields dict, mic_valid).
    """
    if phy[0] != MHDR_JOIN_ACCEPT:
        raise ValueError("not a JoinAccept")
    dec = AES.new(appkey, AES.MODE_ECB).encrypt(phy[1:])
    body, mic = dec[:-4], dec[-4:]
    fields = {
        "appnonce": body[0:3],
        "netid": body[3:6],
        "devaddr": body[6:10][::-1].hex().upper(),
        "dlsettings": body[10],
        "rxdelay": body[11],
        "cflist": body[12:],
    }
    ok = _cmac(appkey, bytes([MHDR_JOIN_ACCEPT]) + body) == mic
    return fields, ok


def next_devaddr(netid, counter):
    """Mint a DevAddr. Top 7 bits are NwkID (low 7 of NetID); the rest is
    ours to allocate. Deterministic so a restart does not collide."""
    nwkid = netid[2] & 0x7F
    return bytes([(nwkid << 1) | ((counter >> 24) & 0x01),
                  (counter >> 16) & 0xFF,
                  (counter >> 8) & 0xFF,
                  counter & 0xFF])


def build_txpk(rxpk, phy_b64, rx_delay_s=JOIN_ACCEPT_DELAY1, power=14):
    """Semtech txpk for RX1: same channel, same datarate, inverted pol.

    tmst is the gateway's own microsecond counter from the uplink plus
    the delay -- absolute wall-clock time is irrelevant and would be
    wrong anyway, since this gateway's clock is not GPS-disciplined.
    """
    return {
        "txpk": {
            "imme": False,
            "tmst": (int(rxpk["tmst"]) + rx_delay_s * 1_000_000) & 0xFFFFFFFF,
            "freq": rxpk["freq"],
            "rfch": 0,
            "powe": power,
            "modu": "LORA",
            "datr": rxpk["datr"],
            "codr": rxpk.get("codr", "4/5"),
            "ipol": True,          # downlinks are inverted-polarity
            "size": len(phy_b64),
            "ncrc": True,          # JoinAccept carries no CRC
            "data": None,          # filled by caller
        }
    }
