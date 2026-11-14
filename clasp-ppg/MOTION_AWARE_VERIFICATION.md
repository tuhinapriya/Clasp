# Motion-aware PaPaGei verifier (experimental)

This module remains portable Python under `clasp-ppg/`; it makes no calls to the Clasp gateway and opens no network service. It does not train or fine-tune PaPaGei. A `MATCH` is an experimental signal result, not authorization.

## Polar SDK streams and timing

The installed and pinned Polar Python SDK implementation was inspected directly. Its Verity Sense connector accepts `ppg_callback` and `acc_callback`. It starts raw PPG and ACC as separate PMD streams, requests 135 Hz four-channel PPG in SDK mode and 52 Hz, 16-bit, ±8 g ACC. ACC rows are decoded as x/y/z milli-g. The SDK's `_active_streams` and empty `stream_errors` indicate control-plane startup only. The adapter reports `streaming=True` only after callbacks deliver samples for every requested stream; `wait_for_streaming()` exposes a bounded readiness check. The Verity Sense SDK supports PPG and accelerometer. Polar's official product notes list ACC rates of 26/52/104/208/416 Hz and PPG options; SDK mode requests only explicitly selected streams. See [Polar Verity Sense SDK capability notes](https://github.com/polarofficial/polar-ble-sdk/blob/master/documentation/products/PolarVeritySense.md) and [SDK mode behavior](https://github.com/polarofficial/polar-ble-sdk/blob/master/documentation/SdkModeExplained.md).

Both PMD callbacks carry device timestamps. Capture stores those timestamps separately from host receive times, and writes `ppg_samples.csv` and `acc_samples.csv`. The SDK's timestamp documentation describes one Polar device timebase for sensor streams; reconstruction of sample instants inside each packet uses the configured nominal rate. Windowing uses the reconstructed device timestamps, not host callback latency. The PPG-to-nearest-ACC sample P95 residual is included per window. This is a practical clock-consistency diagnostic, not proof of exact simultaneous ADC sampling. See [Polar's time system notes](https://github.com/polarofficial/polar-ble-sdk/blob/master/documentation/TimeSystemExplained.md).

### SDK regression comparison and live stream diagnostics (14 Nov 2026)

The previous working PPG-only adapter passed `ppg_callback`, `sdk_mode=True`, and `ppg_sample_rate=135` to the Polar connector. The new adapter keeps that path available and independently enables ACC. Inspection of the pinned SDK showed that PMD Control Point and PMD Data notifications are subscribed once before streams start; Verity Sense stream-start order is SDK mode, PPG, then ACC. `_active_streams` records a successful start command, not receipt of data. The SDK's default reconnect-before-streaming path deliberately disconnects and reconnects before it installs those PMD subscriptions.

The empty-capture regression was in our adapter, not simultaneous PMD streaming: during its intentional BLE refresh, the SDK logs the informational message `Disconnecting...`. The old log handler treated any message containing “disconnect” as a link loss, set the capture abort event, and caused a subsequent capture to exit immediately. Diagnostic listening still saw later packets because it did not use that abort event. The handler now requires an explicit warning/error link-loss message. A unit test verifies that the SDK's informational refresh does not abort the stream; explicit “Link lost” events still invalidate the session. Readiness becomes true only after the requested stream callbacks deliver packets. BLE command acknowledgements alone remain insufficient.

Independent live tests made a fresh BLE connection for each mode and listened for 10 seconds after stream setup. The diagnostics count decoded callbacks and samples and also count raw PMD notifications. They saved no raw sample files. Supported settings came from the connected device's SDK `request_stream_settings()` response:

| Mode | SDK active | Raw PMD notifications | PPG callback packets / samples | PPG rate from device timestamps | ACC callback packets / samples | ACC rate from device timestamps | BLE connected after listen | SDK/callback errors |
|---|---|---:|---:|---:|---:|---:|---|---|
| PPG only, 135 Hz | PPG | 26 | 26 / 1,315 | 135.113 Hz | 0 / 0 | — | Yes | None |
| ACC only, 52 Hz | ACC | 5 | 0 / 0 | — | 5 / 467 | 51.793 Hz | Yes | None |
| PPG + ACC | PPG, ACC | 31 | 26 / 1,307 | 135.120 Hz | 5 / 458 | 51.791 Hz | Yes | None |

All three modes had zero callback errors and empty SDK stream-error maps. Inter-packet missing-sample estimates were zero in these diagnostics. The device reported PPG sample-rate support `[28, 44, 55, 135, 176]`, resolution 22, four channels; ACC support `[26, 52, 104, 208, 416]`, resolution 16, range `[2, 4, 8, 16]`, three axes. PPG and ACC timestamps were carried through the PMD decoder on the common Polar clock. There is no need to reverse the SDK's PPG-before-ACC start order: simultaneous streaming delivered both callback types at their requested rates.

### Guided Wearer 1 placement results

After fixing the abort-event bug, the four fresh, separately confirmed sessions were captured in `data/placement/placement_20261114T224136Z/`. The first motion-rejected normal attempt from the earlier aborted run (`b424c4af7adb442199832543dd24423b`) remains audit-only and was not scored. During the successful normal session, two windows passed quality; its third window was rejected for motion RMS 84.74 mg (the fixed limit is 75 mg). Tighter, looser and repositioned sessions each passed all three windows. Measured packet counts and device-clock rates were:

| Placement | PPG packets / samples | PPG measured Hz | ACC packets / samples | ACC measured Hz | PPG↔ACC P95 across windows |
|---|---:|---:|---:|---:|---:|
| Normal | 87 / 4,321 | 135.126 | 21 / 1,627 | 52.150 | 18.40–18.43 ms |
| Tighter | 87 / 4,335 | 135.120 | 18 / 1,581 | 51.781 | 18.31–18.52 ms |
| Looser | 86 / 4,356 | 135.107 | 19 / 1,663 | 51.783 | 18.20–18.38 ms |
| Repositioned | 86 / 4,322 | 135.113 | 18 / 1,647 | 51.778 | 18.30–18.42 ms |

Each session had three non-overlapping ten-second analysis windows. All windows passed PPG count gates; ACC count loss was estimated at 0.2–0.6% per window. Timestamp residuals passed the fixed 20 ms limit.

The PaPaGei processing path was also corrected without changing the model: at 135 Hz, resampling some 1,351-sample windows yields 1,251 samples due to polyphase ceiling length, so the adapter now trims to the model's 1,250-sample input; the returned one-window embedding is flattened to a 512-vector before normalization. The saved sessions were scored after these fixes using the frozen checkpoint and unchanged 0.80 provisional threshold.

| WEARER_1 placement | Quality-approved windows | PaPaGei per-window cosine similarities | Mean | Windows ≥0.80 | Windows above impostor max (0.9166) |
|---|---:|---|---:|---:|---:|
| Normal (enrollment) | 2/3 | 0.8979, 0.8979 | 0.8979 | 2/2 | 0/2 |
| Tighter | 3/3 | 0.7791, 0.7986, 0.9313 | 0.8363 | 1/3 | 1/3 |
| Looser | 3/3 | 0.6106, 0.3309, 0.6531 | 0.5315 | 0/3 | 0/3 |
| Repositioned | 3/3 | 0.6264, 0.6218, 0.3336 | 0.5273 | 0/3 | 0/3 |

The held-out reference comes from nine non-overlapping windows of the previous WEARER_2 session, scored against this normal enrollment prototype with the same frozen checkpoint. Its window scores were `0.9166, -0.0233, -0.0638, -0.0580, 0.2909, 0.0372, -0.1554, -0.0090, 0.1908` (mean `0.1251`, range `-0.1554–0.9166`). It is PPG-only and has no ACC quality measurements. The impostor session's adjacent windows are not independent trials, and its wide within-session spread limits this small comparison. All four genuine condition means exceed the impostor-window mean, but only one of eleven scored genuine windows exceeds the impostor maximum; the score ranges overlap substantially. Normal is the enrollment session itself and is not an independent genuine test. Tighter's mean exceeds 0.80, but just one of its three windows clears 0.80; the other independent conditions clear the two-window provisional rule in zero windows. This experiment therefore does **not** show genuine similarity consistently above impostor similarity under placement changes. The threshold was not tuned on these recordings.

The tracked, pseudonymous result summary and per-window quality metrics are in [research/placement/PLACEMENT_EXPERIMENT.md](research/placement/PLACEMENT_EXPERIMENT.md) and [research/placement/placement_results.json](research/placement/placement_results.json). Raw captures remain local under the ignored `data/placement/` directory. No model training or Clasp gateway changes were made.

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
if status.connected:
    await sensor.wait_for_streaming(timeout_s=20)
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

The operator gets four separate WEARER_1 sessions (normal, tighter, looser, repositioned). Before each session it must confirm the named wearer and placement in response to displayed instructions; every analysis recording is preceded by a 25-second live-signal stabilization check. A failed session must be repeated after both confirmations. Captures and per-window quality reports remain in the ignored local data directory. Results compare the placement embeddings to the normal enrollment prototype and, if the previous controlled run's local WEARER_2 raw recording is present, report its nine non-overlapping PaPaGei windows as a historical impostor reference. The historical recording has no ACC data and therefore no motion-quality evaluation. Its within-session window similarities are not independent trials. Placement results are descriptive; the same normal session contributes to enrollment and its diagnostic score, so it is not an independent genuine test session. The other three placements are separate captures.

Each accepted/rejected recording is persisted separately with operator-provided wearer label, session ID, timestamp and timestamps for both sensor streams. Do not commit raw captures.

## Validation performed

Pure test suite: `PYTHONPATH=src pytest tests -q` from this directory: 14 tests pass. Coverage includes synchronized three-window segmentation, quiet/moving synthetic fixture checks, fail-closed decisions, disconnect/signal-loss invalidation, the SDK's intentional reconnect log, timestamp-gap persistence, and fixed PaPaGei input sizing. Synthetic fixtures are test-only and are not sensor evidence. The separate BLE diagnostics and placement results above came from real Polar PMD callback data.
