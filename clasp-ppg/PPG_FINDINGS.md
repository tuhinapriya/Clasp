# Polar Verity Sense PPG: what was checked and found

## What the module captures

The module connects to the Polar Sense BLE device and subscribes to its Polar Measurement Data (PMD) photoplethysmography stream. Each sample contains four signed raw integer values: three optical PPG channels and one ambient-light channel. The live runs requested and delivered **135 samples/second**. The values are raw sensor counts, not BPM, an identity score, or a preprocessed pulse waveform.

The CSV contains a sequential sample index, an estimated per-sample sensor-clock timestamp, and the four channel values. The SDK packet timestamp marks the packet's final sample; timestamps inside each packet are reconstructed using the nominal sample rate. These sensor-clock timestamps are not UTC. The recording also stores session metadata locally as JSON. Accelerometer streaming is now disabled; the matcher needs only PPG.

## Hardware checks

- Inspected the Python Polar BLE SDK's Verity Sense connector and raw PMD PPG callback. It decodes the PMD PPG frame into rows of channel values. The SDK source is pinned in `pyproject.toml` at commit `1ca19a44096e34c0ad0edef95db63d1ea051ed0f`.
- Scanned from this Mac and found `Polar Sense 236D0839`.
- The first connection reached the PMD service but macOS returned `Encryption is insufficient` when enabling its notifications. Pairing the sensor in macOS System Settings allowed the Python stream to start. Polar Flow was closed during capture.
- Measured the saved sample interval at about 0.007407407 seconds, or 135.000 Hz.

## Real recordings

All recordings below came from the live Polar sensor. The files remain in the local, git-ignored `data/` directory.

| Participant / condition | Samples | Time covered | Pulse cycles extracted | Capture |
|---|---:|---:|---:|---|
| `participant_1`, baseline | 2,680 | 19.83 s | 21 | `data/participant_1/df4b4e82e3a2470685b22c6c22bc822b/ppg_samples.csv` |
| `participant_2`, impostor | 3,355 | about 24.9 s | 29 | `data/participant_2/0aca17ca431046518030d944b59e70f2/ppg_samples.csv` |
| `participant_1`, rewear | 3,348 | about 24.8 s | 31 | `data/participant_1/cbdba890fec040a8bab8746513599162/ppg_samples.csv` |

For the first recording, all optical channels varied. The channel with the largest standard deviation was optical channel 3 (zero-based index 2, named `ppg_3`): minimum -580,677; maximum -575,515; standard deviation about 1,506 raw counts. The ambient channel changed less. The first capture passed the module's simple quality checks as `GOOD`.

## What was compared

The experimental baseline:

1. Selects the optical channel with the largest standard deviation.
2. Band-pass filters it from 0.5 to 5 Hz.
3. Segments pulse cycles, resamples each cycle to 64 points, and normalizes its shape.
4. Forms a median pulse template and compares templates using cosine distance. **Smaller distances mean more similar shapes.**

The three pairwise scores were:

| Comparison | Label | Distance |
|---|---|---:|
| `participant_1` baseline ↔ `participant_1` rewear | Genuine | 0.0405 |
| `participant_1` baseline ↔ `participant_2` | Impostor | 0.0206 |
| `participant_1` rewear ↔ `participant_2` | Impostor | 0.0088 |

The impostor distances were smaller than the genuine rewear distance. On these recordings, this feature method did **not** distinguish the two participants. Calibration found no threshold that separates the observed genuine and impostor scores (`NO_SEPARATION`). The verifier returns `UNCERTAIN`; it does not accept an impostor match.

This is a very small feasibility check: one baseline session and one rewear session from participant 1, plus one session from participant 2. The pair scores are not false-accept or false-reject rates and do not establish what would happen with longer or additional recordings. More data could show whether this overlap persists, but it would not guarantee that more data alone fixes the method.

## Interpretation and boundaries

- Raw PPG acquisition from this sensor and Mac is proven. The live files contain thousands of varying samples, with the requested channel layout and measured sampling cadence.
- The current morphology matcher is not a useful identity discriminator on this tiny sample. The observed impostor examples were more similar than the genuine rewear example.
- The quality checks detect empty, short, flat, and non-finite recordings. They do not prove skin contact or liveness. Polar documents skin-contact reporting for Verity Sense as unreliable.
- Nothing here changes Clasp's agent, authorization gateway, Stripe integration, or dashboard. PPG remains experimental evidence only; the Clasp gateway owns authorization.
- The repository tests use explicitly synthetic in-memory waveforms only to exercise pure processing code. They are not included as sensor recordings or represented as hardware evidence.

## Reusable continuation prompt

> Continue work only in `clasp-ppg/`; this Mac is developing a portable Python PPG module for the main Clasp app, which will run on one Mac. Read `CLASP_PPG_AGENT_CONTEXT.md`, `clasp-ppg/README.md`, and this findings note first. The Polar Sense is paired to this Mac. The module now captures real four-channel PPG at 135 Hz and does not subscribe to accelerometer data. Existing local, git-ignored recordings are under `clasp-ppg/data/`: participant 1 baseline, participant 1 rewear, and participant 2 impostor. The current band-pass/normalized pulse-shape cosine matcher did not separate them: genuine rewear distance 0.0405, impostor distances 0.0206 and 0.0088. Calibration correctly reports `NO_SEPARATION` and `verify()` returns `UNCERTAIN`. Do not change the decision to MATCH unless new independently held-out real recordings show a defensible separation. Inspect the raw signals and research methods before changing features; compare multiple optical channels and simple per-beat morphology features if useful. Do not use accelerometer data, generated input as real evidence, or alter Clasp's agent, gateway, Stripe integration, or dashboard. If collecting more people, use consent and clear participant/session labels. Report exact capture conditions, counts, score distributions, and limitations; do not claim security-grade accuracy.
