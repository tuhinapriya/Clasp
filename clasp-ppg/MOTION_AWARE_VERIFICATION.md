# Motion-aware PaPaGei verifier (experimental)

This module remains portable Python under `clasp-ppg/`; it makes no calls to the Clasp gateway and opens no network service. It does not train or fine-tune PaPaGei. A `MATCH` is an experimental signal result, not authorization.

## Polar SDK streams and timing

The installed and pinned Polar Python SDK implementation was inspected directly. Its Verity Sense connector accepts `ppg_callback` and `acc_callback`. It starts raw PPG and ACC as separate PMD streams, requests 135 Hz four-channel PPG in SDK mode and 52 Hz, 16-bit, ±8 g ACC. ACC rows are decoded as x/y/z milli-g. A stream is considered available only when the SDK's `_active_streams` set contains the respective PMD type and the stream has no start error; by default both are required. The Verity Sense SDK supports PPG and accelerometer. Polar's official product notes list ACC rates of 26/52/104/208/416 Hz and PPG options; SDK mode requests only explicitly selected streams. See [Polar Verity Sense SDK capability notes](https://github.com/polarofficial/polar-ble-sdk/blob/master/documentation/products/PolarVeritySense.md) and [SDK mode behavior](https://github.com/polarofficial/polar-ble-sdk/blob/master/documentation/SdkModeExplained.md).

Both PMD callbacks carry device timestamps. Capture stores those timestamps separately from host receive times, and writes `ppg_samples.csv` and `acc_samples.csv`. The SDK's timestamp documentation describes one Polar device timebase for sensor streams; reconstruction of sample instants inside each packet uses the configured nominal rate. Windowing uses the reconstructed device timestamps, not host callback latency. The PPG-to-nearest-ACC sample P95 residual is included per window. This is a practical clock-consistency diagnostic, not proof of exact simultaneous ADC sampling. See [Polar's time system notes](https://github.com/polarofficial/polar-ble-sdk/blob/master/documentation/TimeSystemExplained.md).

Live smoke check (14 Nov 2026): this Mac connected to the real Polar Sense 236D0839; the SDK acknowledged both PMD start commands at requested 135 Hz PPG and 52 Hz ACC. The first guided normal-placement attempt (`b424c4af7adb442199832543dd24423b`) ended with zero PPG and ACC packets after the BLE link dropped during stabilization. On reconnect, a non-recording readiness check found the BLE link and both SDK start acknowledgements, but no PPG or ACC notifications in five seconds. The adapter therefore reports `streaming=False` with `no data notifications arrived` instead of treating command acknowledgement as a working stream. The first attempt is retained/rejected; it yielded no clock-alignment or similarity score. WEARER_1 placement scoring was not completed. No genuine placement ranking is claimed until fresh clean sessions are collected. Automated fixtures are explicitly synthetic and test algorithms only.

## Quality gate

Each analysis window is 10 seconds, non-overlapping, with three windows per 32-second capture (the two-second margin covers asynchronous stream start). A window is rejected if it fails any of these experimental heuristics:

| Measurement | Gate |
|---|---:|
| PPG and ACC sample counts | At least 98% of nominal PPG samples and 95% of nominal ACC samples |
| PPG amplitude | Standard deviation ≥25 raw counts and peak-to-peak ≥100 counts |
| PPG rail clipping | Reject int32 rails or sustained repeated min/max plateaus (≥max(5 samples, 4% of the sample rate)); no undocumented ADC full-scale is assumed |
| Pulse cycles | At least 5 pulse intervals, estimated rate 40–170 BPM, interval CV ≤0.50 |
| ACC gravity magnitude | Median 700–1300 mg |
| ACC dynamic movement | High-pass (0.5 Hz) dynamic magnitude RMS ≤75 mg and P95 ≤250 mg |
| Cross-stream timestamps | PPG-to-nearest-ACC timestamp P95 ≤20 ms |

These thresholds are deliberately conservative engineering heuristics, not thresholds established by a clinical or biometric validation study. Motion filtering is a proxy for movement, and cannot detect every placement or physiological artifact. Window-level reasons and raw measurements are returned. There is no silent acceptance of a rejected window.

## Live verifier API

```python
from clasp_ppg.device import PolarPPG
from clasp_ppg.verifier import PaPaGeiEmbedder, enroll_capture, verify_live

embedder = PaPaGeiEmbedder("/path/to/papagei-foundation-model", "weights/papagei_p.pt")
sensor = PolarPPG()  # requests real PPG+ACC from a Verity Sense
status = await sensor.connect()
if not status.connected or not status.streaming:
    raise RuntimeError(status.error or "Polar PPG+ACC unavailable")
enrollment_capture = await sensor.capture("WEARER_1", 32, session_type="enrollment")
profile = enroll_capture(enrollment_capture, embedder)  # at least two clean windows
result = await verify_live(sensor, profile, embedder, threshold=0.80)
print(result["decision"], result["windows"], result["rejection_reasons"])
```

Embedding preprocessing follows the existing frozen PaPaGei-P evaluation: fixed `ppg_3`, per-window z-score, fourth-order Chebyshev II 0.5–12 Hz bandpass, 50 ms smoothing at ≥75 Hz, polyphase resampling to 125 Hz, one 1250-sample tensor, frozen 512-dimensional output, L2 normalization, cosine score. The 0.80 default is provisional and configurable; it is not production-calibrated. It was set before this placement study and must not be adjusted using its test sessions.

Decision rule: at least two quality-approved windows scoring at or above the threshold yields `MATCH`; at least two clean windows below it yields `MISMATCH`; fewer than two clean windows yields `UNCERTAIN`. Bad/missing signal is never an identity mismatch. The output includes all three windows' score, quality measurements, reasons, and approved-window count. A matching result creates an in-memory active-session handle. Polar disconnect or a detected loss of pulse-like variation invalidates it synchronously in the stream callback. `PolarPPG.disconnect_event` is also exposed to interrupt an in-progress capture.

Physiological-signal presence is a basic short-term raw-amplitude variation check, not a reliable skin-contact or liveness sensor. No biometric-only system should be treated as secure authorization.

## Guided placement experiment

Run from `clasp-ppg/` after installing the Polar package, the `papagei` extra (PyTorch), official PaPaGei source, and checkpoint. `pip install -e '.[papagei]'` installs PyTorch; the official source and pretrained file remain local paths:

```sh
clasp-ppg placement-experiment \
  --papagei-source /path/to/papagei-foundation-model \
  --checkpoint weights/papagei_p.pt
```

The operator gets four separate WEARER_1 sessions (normal, tighter, looser, repositioned). Before each session it must confirm the named wearer and placement in response to displayed instructions; every recording is preceded by a 25-second stabilization period. A failed session must be repeated after both confirmations. Captures and per-window quality reports remain in the ignored local data directory. Results compare the placement embeddings to the normal enrollment prototype and, if the previous controlled run's local WEARER_2 raw recording is present, report that separate historical PaPaGei similarity as an impostor reference. The historical recording has no ACC data and therefore no motion-quality evaluation. The report explicitly records that limitation and whether each genuine placement's mean similarity is above that reference. If its local raw sample data is absent, no impostor ranking is claimed. Placement results are descriptive; the same normal session contributes to enrollment and its diagnostic score, so it is not an independent genuine test session. The other three placements are separate captures.

Each accepted/rejected recording is persisted separately with operator-provided wearer label, session ID, timestamp and timestamps for both sensor streams. Do not commit raw captures.

## Validation performed

Pure test suite: `PYTHONPATH=src pytest tests -q` from this directory. Ten tests cover existing capture/matcher processing plus synchronized three-window segmentation, quiet/moving synthetic fixture quality decisions, disconnect/signal-loss invalidation, and 2-of-3 / insufficient-clean-window decisions. The synthetic fixtures are not sensor recordings. A real BLE link and PMD start acknowledgement succeeded, but the smoke test detected no PPG/ACC data notifications, so live packet capture, measured timestamp alignment, and placement scores remain unavailable until the device streams packets.
