# Clasp PPG module

Portable, local Python code for Polar Verity Sense raw PPG capture, timestamped accelerometer capture, an experimental one-to-one morphology matcher, and a PaPaGei window verifier. It has no dependency on Clasp's gateway, UI, agent, Stripe, a LAN, or another computer. Keep this folder together when copying it into the main Clasp repository.

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
    if status.connected:
        await sensor.wait_for_streaming(timeout_s=20)
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

`PolarPPG.status` exposes `connected`, `streaming`, `acc_streaming`, `physiological_signal_present`, `device_name`, `device_address`, `last_sample_at`, `signal_quality`, and `error`. `capture()` returns a `Capture` and can write `ppg_samples.csv`, `acc_samples.csv`, and `metadata.json`. The CSVs keep sample index, estimated Polar sensor-clock timestamp, and raw integer values. Within a PMD packet, timestamps are reconstructed from the packet's last-sample timestamp and nominal configured sample rate; they are not claimed as UTC. `assess_quality()` marks short, flat, non-finite, or empty PPG recordings unusable. `enroll()`, `calibrate()`, and `verify()` only operate on captures supplied to them; they never invent live input.

## Experimental matcher and evaluation

The baseline filters one optical channel between 0.5 and 5 Hz, detects pulse cycles, resamples each cycle to 64 points, normalizes its shape, and compares median pulse templates with cosine distance. This is an explainable morphology baseline, informed by PPG authentication studies that use per-beat features and explicitly test cross-session stability. It is not a validated identity system. `verify()` returns `UNCERTAIN` until `calibrate()` receives held-out genuine and impostor recordings. Calibration sets a threshold only when the observed maximum genuine score is below the minimum impostor score; overlap yields `NO_SEPARATION` and verification stays `UNCERTAIN`.

Record separate sessions for the same participant, including sensor removal and reattachment:

```sh
clasp-ppg record --participant demo_person --duration 60 --session-type rewear
clasp-ppg record --participant other_person --duration 60 --session-type impostor
clasp-ppg evaluate --data-dir data
```

`evaluate_labeled()` and the CLI report pairwise genuine/impostor morphology scores and identify pairs involving a `rewear` session. This is a descriptive feasibility report, not an FAR/FRR estimate. In the initial live two-participant check, one genuine rewear score (0.0405) exceeded both impostor scores (0.0088, 0.0206), so there is no separating threshold; this baseline currently cannot distinguish these samples. Do not train/test on adjacent windows as if they were independent people. The Verity Sense reports skin contact unreliably; signal quality and connection state cannot establish liveness, consent, or identity. Keep the Clasp gateway as the final authorization point and fail closed when PPG is missing or poor.

## Guided controlled biometric experiment

The interactive command collects four fresh, labeled 90-second sessions in order: `WEARER_1` enrollment, `WEARER_1` rewear verification, `WEARER_2` impostor, and `WEARER_1` second rewear verification. Before every stabilization and recording attempt it displays the requested wearer and placement instructions, then requires the operator to type the exact wearer label and `SAME_LOCATION`. The software never infers who is wearing the sensor. Each confirmed placement gets a 20–30 second live stabilization sample (25 seconds by default) that is discarded; the operator must then type `START` for the 90-second recording. Every retake repeats these checks.

Run it in an environment that has both the Polar BLE dependencies and the PaPaGei model dependencies installed. Clone PaPaGei and use its official checkpoint in the ignored local `weights/` directory:

```sh
git clone https://github.com/Nokia-Bell-Labs/papagei-foundation-model /tmp/papagei-official
# In the experiment Python environment, install clasp-ppg plus torch, scikit-learn, matplotlib, and tqdm.
python -m pip install -e . torch scikit-learn matplotlib tqdm
mkdir -p weights
curl -L https://zenodo.org/api/records/13983110/files/papagei_p.pt/content -o weights/papagei_p.pt
clasp-ppg experiment --papagei-source /tmp/papagei-official
```

The historical controlled command records raw PPG locally, summarizes measured sample clock, missing packet samples, raw amplitude/clipping, pulse-cycle regularity, usable duration, and valid 10-second windows, then rejects poor sessions and asks for a retake. That earlier run did not capture accelerometer data; the new live motion-aware verifier does. The historical command's default quality gate requires at least 85 seconds usable, sample clock within 2% of its configured rate, estimated missing samples no greater than 0.5%, no integer-rail or sustained-extreme clipping indication, at least 25 identifiable pulse cycles, at least 8 valid 10-second windows, pulse interval coefficient of variation no greater than 0.50 when measurable, and no PPG stream errors. These are conservative experiment acceptance checks, not identity thresholds.

Accepted and rejected attempts are kept separately under a unique `data/controlled_ppg/controlled_<timestamp>_<id>/` folder. Only four accepted sessions are compared. The command writes `CONTROLLED_PPG_EVALUATION.md` and `results.json` there, with PaPaGei cosine similarity, handcrafted normalized pulse-template distance, session quality, usable windows, and genuine-versus-impostor ranking. No threshold is fit; live verification remains fail-closed. Do not commit the raw recordings or results containing participant biometric data.

If a run is interrupted after one or more accepted sessions, resume only that run with `--resume-run-id controlled_<timestamp>_<id>`. The command validates accepted session labels, skips those sessions from the current run, and never imports recordings from another run. Terminal confirmations are read without blocking the BLE event loop, and the device reconnects before a new stabilization sample if its connection was lost.

## Motion-aware verifier and placement experiment

The local Polar adapter also requests the real Verity Sense accelerometer stream, saves timestamped raw PPG and ACC, and exposes the experimental PaPaGei three-window verifier. It checks that actual PPG and ACC packets arrive before reporting `streaming=True`. Run the guided placement study with `clasp-ppg placement-experiment --papagei-source /path/to/papagei-foundation-model`. Its provisional threshold, experimental motion/PPG quality gate, and current live-stream smoke-test outcome are documented in [MOTION_AWARE_VERIFICATION.md](MOTION_AWARE_VERIFICATION.md); do not treat its MATCH output as production authorization.

To compare Polar PMD streams independently without saving raw samples, run `clasp-ppg diagnose-streams --duration 10`. It tests PPG-only 135 Hz, ACC-only 52 Hz, then PPG+ACC, and reports callback counts, decoded samples, raw PMD notifications, negotiated settings, BLE status and SDK stream errors for each fresh connection. The latest live placement results are summarized in [research/placement/PLACEMENT_EXPERIMENT.md](research/placement/PLACEMENT_EXPERIMENT.md); raw PPG/ACC samples remain local and ignored by Git.

## Data and privacy

Raw PPG and enrollment templates are biometric data. The default CLI writes locally under `data/`; do not commit captures, templates, device addresses, or identifying participant metadata. Obtain informed consent for every participant. Before sharing a capture, redact the device address from `metadata.json`. Captures and templates are not encrypted by this prototype. Derived research reports may be shared only with explicit approval and pseudonymous wearer labels.

## Research references

- Polar, [Verity Sense capabilities and PPG sampling rates](https://github.com/polarofficial/polar-ble-sdk/blob/master/documentation/products/PolarVeritySense.md). The documented SDK supports raw PPG; SDK mode provides higher sampling-rate options and disables device-side HR/PPI algorithms.
- Lovisotto et al., [Seeing Red: PPG Biometrics Using Smartphone Cameras](https://openaccess.thecvf.com/content_CVPRW_2020/papers/w48/Lovisotto_Seeing_Red_PPG_Biometrics_Using_Smartphone_Cameras_CVPRW_2020_paper.pdf). The study extracts heartbeat features and evaluates across multiple sessions; its camera signal and results do not transfer directly to the Verity Sense.
- [Unofficial Python Polar BLE SDK source and license notices](https://github.com/PhelaPoscam/Polar-Python-SDK).

Run pure processing tests with `python -m pytest` from this directory. These tests use explicitly synthetic in-memory waveforms for algorithm checks; they are never written as recordings or presented as sensor evidence.

## Continuous experimental verification

The continuous runtime needs an existing `primary-user` profile saved by `profile_store.save_profile` after explicit enrollment. It does not create a profile. It maintains one Polar connection, buffers PPG and ACC on the sensor clock, and evaluates non-overlapping 10-second windows. It retains nine window results in memory. The most recent three windows must all pass quality checks before the existing two-of-three PaPaGei decision rule can return a decision; the threshold remains 0.80. A rejected latest window, missing samples, stream failure, disconnect, or stale stream makes the result uncertain. The model and threshold are experimental and are not calibrated for secure authorization.

```sh
python -m clasp_ppg.continuous --profile /path/to/primary-user-ppg.json \
  --papagei-source /path/to/papagei-foundation-model --checkpoint weights/papagei_p.pt
```

Each line on stdout is a JSON decision with identity, score, quality, timestamps, reason and up to nine recent windows. The result expires after 15 seconds. The current runtime stops on a connection or stream failure; restart it after reconnecting the device. Do not treat a connection or received samples as proof of identity.

## Clasp worker protocol

Clasp starts one worker with `POST /api/hardware/polar/start` and stops it with `POST /api/hardware/polar/stop` on its loopback API. The worker owns the sole Polar connection. The default profile is `<Clasp>/.clasp/ppg-profile.json`; it must already be an explicitly enrolled `primary-user` profile. With no profile, the worker can report actual stream availability but cannot produce identity evidence. To configure a separately downloaded PaPaGei source tree, set `CLASP_PAPAGEI_SOURCE` in Clasp's environment; `CLASP_PPG_PYTHON`, `CLASP_PPG_PROFILE`, `CLASP_PAPAGEI_CHECKPOINT`, and `CLASP_PPG_DEVICE` optionally override the other paths or device. Restart Clasp after changing configuration. The Python environment must have the `clasp-ppg` package and model dependencies installed.

Protocol version 1 uses one JSON object per stdout line: `ready` and `profile` report enrollment; `device` reports connection, actual packet streaming, packet counts, and observation time; `verification` carries decision, identity, similarity, quality, verification time and expiry; `error` reports failures; `stopped` ends the session. A single stdin line `{"type":"shutdown","version":1}` requests shutdown. Diagnostic output goes to stderr. Clasp invalidates evidence on stream failure, invalid messages, missing profile, heartbeat timeout, worker exit, and expiry. This experimental matcher is not calibrated for secure authentication.
