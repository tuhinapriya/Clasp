# PPG subsystem status

## Verified on this Mac

- Inspected the upstream Polar BLE Python SDK source at commit `1ca19a44096e34c0ad0edef95db63d1ea051ed0f`; its `PolarVeritySense` connector starts a decoded PMD PPG callback. The SDK documentation describes four PPG channels (three optical plus ambient), 22-bit values, and rates including 135 Hz in SDK mode.
- Installed this SDK in an isolated Python 3.14 virtual environment and inspected `monitor-polar --help`.
- BLE scan saw `Polar Sense 236D0839` and its Polar Measurement Data GATT service. The first stream attempt failed with CoreBluetooth `CBATTErrorDomain Code=15: Encryption is insufficient`; after the user paired it in macOS Bluetooth settings, the stream started.
- Captured 2,680 real PPG samples over 19.83 sensor-clock seconds at a measured 135.000 Hz. The decoder returned four channels: three optical channels plus ambient. Each optical channel varied; selected channel 3 (zero-based index 2) had raw values from -580,677 to -575,515 ADC counts.
- Quality assessment marked the first capture `GOOD` and extracted 21 pulse cycles. A same-session self-comparison scored 0.0 before calibration was tightened; that self-check is not biometric evidence.
- Recorded a second participant capture (3,355 samples, 29 extracted cycles) and one more first-participant rewear capture (3,348 samples, 31 cycles). All three sessions streamed at 135 Hz.
- Pairwise scores were genuine rewear 0.0405, impostors 0.0088 and 0.0206. An impostor was closer to the enrollment template than the genuine rewear capture; this baseline has no useful separation on the current samples.
- Independent calibration returned `NO_SEPARATION` and no threshold. Verification against the second participant returned `UNCERTAIN` with reason `no_genuine_impostor_separation`, as intended for this overlap.
- This module uses the SDK's real PPG callback. It has no synthetic-data fallback. The recorded Python package expects real decoded samples and will report an empty recording if the sensor delivers none.
- Accelerometer subscription and storage are disabled; the matcher uses PPG only.

## Not yet verified

- Long-duration stream stability and device disconnect handling.
- Whether morphology features can distinguish more sessions/participants; current live scores overlap in the wrong order and do not support identity decisions.
- Device-address privacy beyond local storage; the current capture metadata includes the BLE address and must be redacted before sharing.

## Hardware steps

The sensor is paired with this Mac; close Polar Flow before capture. Record more independent sessions per participant and additional consented participants to investigate the current score overlap:

```sh
cd clasp-ppg
.venv/bin/python -m clasp_ppg.cli record --participant participant_1 --duration 60 --session-type rewear
.venv/bin/python -m clasp_ppg.cli record --participant participant_2 --duration 60 --session-type impostor
.venv/bin/python -m clasp_ppg.cli evaluate --data-dir data
```

Collect several independent sessions from each consented participant, including removal and reattachment. The current data are only a short feasibility check; do not report biometric accuracy from these scores.

## Limitations

PPG morphology is experimental, placement and motion sensitive, and not proof of identity or liveness. The Verity Sense's skin-contact flag is documented as unreliable. A Bluetooth connection alone is presence evidence only. The matcher is a research baseline, not payment-grade authentication or a Clasp authorization decision.
