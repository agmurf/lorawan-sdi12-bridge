# lorawan-sdi12-bridge

A second, independent receive path for a river gauge, built so that flood
data keeps flowing when the primary LoRaWAN backbone does not.

    Milesight EM411-RDL  --LoRa 922-923.4 MHz-->  RAK2245 / Pi Zero 2 W
                                                        |
                                            local network server, decode
                                                        |
                                              river_state.json
                                                        |
                                        SDI-12 slave (1200 baud, 7E1)
                                                        |
                                              ELPRO ERT-A2  --VHF 151.5 MHz-->  ALERT network

The gauge already reports to a commercial LoRaWAN network. This is a
parallel path that shares only the sensor: its own gateway, its own
network server, its own decode, feeding a radio that does not depend on
the internet. If the backbone fails mid-flood, height keeps reaching the
ALERT network.

---

## Parts

| Part | Role | Notes |
|---|---|---|
| **Milesight EM411-RDL** | radar river level sensor | LoRaWAN AS923, OTAA. 80 mm radar, reports distance to water |
| **RAK2245 Pi HAT** | LoRa concentrator | SX1301 baseband + 2x SX1257 radios, 8 multi-SF channels |
| **Raspberry Pi Zero 2 W** | gateway host | runs the packet forwarder, network server, decoder and SDI-12 slave |
| **Raspberry Pi 4** | console host | drives the ERT-A2 over USB CDC-ACM (`/dev/ttyACM0`) |
| **ELPRO ERT-A2** | ALERT field station | firmware v1.7, hardware Rev4.A, Tx 151.5 MHz at 5 W |
| SDI-12 interface | gateway to ERT-A2 | bit-banged on GPIO via `pigpio`, 1200 baud 7E1 |

### Sensor geometry

The height calculation is **derived, not chosen**:

    height = (sensor_height - measured_distance) / 1000

    sensor height    1946.2 mm     measured to the water datum
    blind zone       0.15 m        radar minimum range
    valid band       -0.054 m  ..  1.79 m

Both ends of that band come from hardware limits: the upper bound is
`sensor height - blind zone`, the lower is `sensor height - measurement
range`. Earlier both bounds were **invented**, and both were wrong -- the
upper by 240 mm, and the lower rejected an entire falling tide while the
ERT-A2 sat retransmitting a frozen level over the radio as though it were
live. Do not guess a physical constant. Derive it or ask.

---

## The thing that made it work

**The gateway must listen on 922.0 - 923.4 MHz. Not the RAK default.**

The EM411 is AS923: the two AS923-1 defaults (923.2, 923.4) plus six
channels added *downward*. RAK's AS923 preset adds its six *upward*
(923.6 - 924.6). The two plans overlap on exactly the two defaults, so a
stock gateway hears **2 uplinks in every 8**.

    stock plan   923.2 - 924.6      25-29 % reception
    this plan    922.0 - 923.4      89-94 % reception

That single fact explains every symptom that had been chased for weeks:
losses that were binary rather than graded, a completely empty 12 dB SNR
gap between the weakest decoded frame and the noise floor, and losses
uncorrelated with the primary network's signal strength. Antenna gain,
spreading factor and path loss were all investigated first. All were
wrong.

`channel_plan.py` enforces the plan as an `ExecStartPre` on the packet
forwarder, because `global_conf.json` belongs to the RAK installer and any
update silently restores the stock plan.

---

## Code

| File | What it does |
|---|---|
| `gateway/channel_plan.py` | rewrites the 8-channel plan before the forwarder starts; idempotent, fails safe |
| `gateway/local_ns_logger.py` | minimal LoRaWAN network server: MIC check, FCnt32 reconstruction, AppSKey decrypt |
| `gateway/em411_decoder.py` | Milesight TLV payload parser |
| `gateway/em411_height.py` | distance to height, with the derived validity band |
| `gateway/sdi12_slave.py` | SDI-12 protocol layer: `?!`, `aI!`, `aM!`, `aD0!` |
| `gateway/sdi12_pigpio.py` | bit-level 1200 baud 7E1 with break detection |
| `gateway/healthcheck.py` | 14 checks, one verdict, exit 0/1/2 |
| `gateway/rxstats.py` | reception measured **by frame counter**, never wall-clock |
| `gateway/sdi12_export.py` | exports what was actually served, as CSV, for correlation |
| `ert/ert.py` | ERT-A2 console driver with a credential guard |
| `ert/ert_login.py` | interactive login; refuses to run without a TTY |
| `ert/diagnostics/echo_test.py` | proves the console drops burst serial input |

### Measure by frame counter, not by clock

`rxstats.py` computes reception from the device's own frame counter. That
distinction is what eventually solved this: it separates *"we missed it"*
from *"the sensor never sent it"*, and those have completely different
fixes. The sensor has never once skipped a transmission. Every loss in
this project was at the receiver.

### Instrument the silent paths

MIC failures, FPort rejections and CRC-flagged frames were all being
dropped with no log line, so a key mismatch would have looked exactly like
bad reception. They are logged now, with a `recovered` counter for frames
the CRC condemned and the MIC vouched for.

Worse, `rxstats.py` originally could not see frames that were received and
then *rejected*, because the INVALID log line carried no frame counter --
so an over-tight validity band showed up as "poor reception". **The metric
concealed the very fault it should have exposed.**

---

## Keys

There are none in this repository. The working gateway loads them from
`device_keys.json` (chmod 600, gitignored). `local_ns_logger.py` here has
its key literals and device identifiers replaced with placeholders.

If you deploy this, keep keys out of source. An `AppKey` is enough to
impersonate the sensor it belongs to.

---

## Known limits

* **ALERT cannot express "no valid reading."** The value field is 11 bits,
  so a negative sentinel cannot be transmitted, and the ERT-A2 holds
  last-good on both a sentinel *and* silence. A dead sensor therefore
  transmits a plausible frozen level indefinitely. Staleness is flagged in
  bit 12 of the sensor ID, which is where standard receivers do not look.
* **11-bit ceiling.** With Scale 1000 the maximum transmittable height is
  2.047 m. The band tops out at 1.79 m, so this is not currently binding.
* **ADR is enabled** on the device, so the primary network can move its
  data rate underneath this path at any time.

---

## Licence

MIT. See `LICENSE`.

This is field engineering on a live flood-warning path, published because
the reasoning may be useful to others doing similar work. It is not a
product, and it carries no warranty of any kind. Do not put it in the path
of a public warning without your own verification.
