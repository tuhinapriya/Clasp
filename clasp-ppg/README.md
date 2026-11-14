# Clasp PPG module

Portable, local Python code for Polar Verity Sense raw PPG capture and an experimental one-to-one morphology matcher. It captures PPG only; accelerometer data is not required. It has no dependency on Clasp's gateway, UI, agent, Stripe, a LAN, or another computer. Keep this folder together when copying it into the main Clasp repository.

## Hardware and setup

The live acquisition adapter uses the unofficial `polar-ble-sdk` Python project (Bleak plus its Polar Measurement Data decoder). Polar's official SDK documents Verity Sense PPG streaming, but its maintained SDKs target Android and iOS. The Python project is pinned to a reviewed source commit; its PMD decoder includes Polar licensed material. Review its `NOTICE` and Polar SDK license before redistribution. This module does not copy that decoder into its own source.

Python 3.10 or newer is supported by the dependency; Python 3.12 or 3.13 is a conservative choice for portability. On macOS, install Apple's command line tools if your Python distribution needs them for BLE permissions. Keep Polar Flow closed during capture. Put the Verity Sense in sensor mode (heart icon / blue side LED), charge it, and wear it snugly on the upper arm or forearm. The sensor may be discoverable before it is bonded to the Mac.

From this directory:

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev]'
clasp-ppg scan --timeout 12
clasp-ppg record --participant demo_person --duration 60 --session-type baseline
```

If scan sees no device, check sensor mode, battery, Bluetooth permissions, and that Polar Flow or another BLE app has released the connection. If macOS reports `Encryption is insufficient` while enabling PMD notifications, pair Polar Sense with this Mac in System Settings → Bluetooth, close Polar Flow, and retry. Bleak's macOS backend does not expose an explicit pairing call; the sensor may require a system bond for its encrypted PMD stream.

Raw PPG streaming through SDK mode requests 135 Hz and four channels (three optical plus ambient); the actual stream setup can fall back to 55 Hz on older firmware or if SDK mode is refused. The saved metadata records the selected rate and SDK errors. A connection alone is not proof of skin contact.

## Python API

```python
import asyncio
from clasp_ppg import EnrollmentStore, PolarPPG, assess_quality, calibrate, enroll, verify
from clasp_ppg.storage import capture_matrix, load_capture

async def record():
    sensor = PolarPPG()
    status = await sensor.connect()
    if not status.connected or not status.streaming:
        raise RuntimeError(status.error or "PPG stream unavailable")
    try:
        return await sensor.capture(
            "demo_person", 60, session_type="baseline", output_dir="data"
        )
    finally:
        await sensor.disconnect()

first_session = asyncio.run(record())
store = EnrollmentStore("data/templates")
enroll("demo_person", first_session, store)

# Collect one held-out genuine rewear session and one held-out impostor session.
genuine_session = load_capture("data/demo_person/REWEAR_SESSION_ID")
impostor_session = load_capture("data/other_person/IMPOSTOR_SESSION_ID")
calibration = calibrate("demo_person", [genuine_session], [impostor_session], store)

# Collect a fresh verification capture the same way; keep it separate from enrollment and calibration.
second_session = load_capture("data/demo_person/VERIFICATION_SESSION_ID")
quality = assess_quality(capture_matrix(second_session), second_session.sampling_rate_hz)
result = verify("demo_person", second_session, store)
print(quality.label, result.decision, result.score, result.reason)
```

`PolarPPG.status` exposes `connected`, `streaming`, `device_name`, `device_address`, `last_sample_at`, `signal_quality`, and `error`. `capture()` returns a `Capture` and can write `ppg_samples.csv` plus `metadata.json`. The CSV keeps sample index, estimated sensor-clock timestamp, and raw integer channel values. Within a PMD packet, timestamps are reconstructed from the packet's last-sample timestamp and nominal configured sample rate; they are not claimed as UTC. `assess_quality()` marks short, flat, non-finite, or empty recordings unusable. `enroll()`, `calibrate()`, and `verify()` only operate on captures supplied to them; they never invent live input.

## Experimental matcher and evaluation

The baseline filters one optical channel between 0.5 and 5 Hz, detects pulse cycles, resamples each cycle to 64 points, normalizes its shape, and compares median pulse templates with cosine distance. This is an explainable morphology baseline, informed by PPG authentication studies that use per-beat features and explicitly test cross-session stability. It is not a validated identity system. `verify()` returns `UNCERTAIN` until `calibrate()` receives held-out genuine and impostor recordings. Calibration sets a threshold only when the observed maximum genuine score is below the minimum impostor score; overlap yields `NO_SEPARATION` and verification stays `UNCERTAIN`.

Record separate sessions for the same participant, including sensor removal and reattachment:

```sh
clasp-ppg record --participant demo_person --duration 60 --session-type rewear
clasp-ppg record --participant other_person --duration 60 --session-type impostor
clasp-ppg evaluate --data-dir data
```

`evaluate_labeled()` and the CLI report pairwise genuine/impostor morphology scores and identify pairs involving a `rewear` session. This is a descriptive feasibility report, not an FAR/FRR estimate. In the initial live two-participant check, one genuine rewear score (0.0405) exceeded both impostor scores (0.0088, 0.0206), so there is no separating threshold; this baseline currently cannot distinguish these samples. Do not train/test on adjacent windows as if they were independent people. The Verity Sense reports skin contact unreliably; signal quality and connection state cannot establish liveness, consent, or identity. Keep the Clasp gateway as the final authorization point and fail closed when PPG is missing or poor.

## Data and privacy

Raw PPG and enrollment templates are biometric data. The default CLI writes locally under `data/`; do not commit captures, templates, device addresses, or other participant information. Obtain informed consent for every participant. Before sharing a capture, redact the device address from `metadata.json`. Captures and templates are not encrypted by this prototype.

## Research references

- Polar, [Verity Sense capabilities and PPG sampling rates](https://github.com/polarofficial/polar-ble-sdk/blob/master/documentation/products/PolarVeritySense.md). The documented SDK supports raw PPG; SDK mode provides higher sampling-rate options and disables device-side HR/PPI algorithms.
- Lovisotto et al., [Seeing Red: PPG Biometrics Using Smartphone Cameras](https://openaccess.thecvf.com/content_CVPRW_2020/papers/w48/Lovisotto_Seeing_Red_PPG_Biometrics_Using_Smartphone_Cameras_CVPRW_2020_paper.pdf). The study extracts heartbeat features and evaluates across multiple sessions; its camera signal and results do not transfer directly to the Verity Sense.
- [Unofficial Python Polar BLE SDK source and license notices](https://github.com/PhelaPoscam/Polar-Python-SDK).

Run pure processing tests with `python -m pytest` from this directory. These tests use explicitly synthetic in-memory waveforms for algorithm checks; they are never written as recordings or presented as sensor evidence.
