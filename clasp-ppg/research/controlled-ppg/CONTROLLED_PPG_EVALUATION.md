# Controlled PPG evaluation — controlled_20261114T213844Z_9207a4

## Protocol and provenance

Four new, operator-labeled 90-second Polar Verity Sense recordings were collected in this run. Wearer identity was explicitly confirmed before each recording; it was never inferred by software. Each session used the same instructed upper-arm placement. A 25-second stabilization sample was discarded before each actual recording. Rejected attempts, if any, are retained under `rejected/` and were not scored. No earlier captures were reused.

The raw recordings are in this run's `accepted/` directory. The wearable stream records PPG only; accelerometer motion assessment was unavailable. PaPaGei-P and the handcrafted morphology distance were both evaluated per session. PaPaGei similarity is cosine similarity (larger is closer); baseline distance is normalized pulse-template cosine distance (smaller is closer). No threshold was fitted on these sessions. PaPaGei source commit: `0c537dad4d2850e15b724260de820dd68d77f0b0`; checkpoint SHA-256: `3a6850961af527cbb2e476d3ad0bb374ae86ed86ffdd55b0b0d7e2f49451518e`.

## Session quality

| Session | Operator label | Session ID | Quality | Raw samples | Measured clock Hz | Estimated missing | Usable seconds | PPG p-p (SD) counts | Pulse / interval CV | PaPaGei windows | Baseline cycles |
|---|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
| enrollment | WEARER_1 | 6110dab9957f4c0d8ef1a8ee948754bc | GOOD | 12149 | 135.108 | 0 | 90.0 | 4793 (1191) | 79.41 BPM / 0.1676 | 8 | 121 |
| verification_1 | WEARER_1 | dbc94625aa38431eb16e2804651f642a | GOOD | 12158 | 135.105 | 0 | 90.1 | 8128 (2139) | 81.82 BPM / 0.1753 | 9 | 122 |
| impostor | WEARER_2 | 5439dc4d39a4418da6f2d92720528e7f | GOOD | 12198 | 135.130 | 0 | 90.4 | 29563 (5468) | 81.0 BPM / 0.0857 | 9 | 121 |
| verification_2 | WEARER_1 | 21cd6d8f823e48bb83018c7f3cc246f5 | GOOD | 12174 | 135.115 | 0 | 90.2 | 2780 (574) | 86.17 BPM / 0.2067 | 9 | 129 |

Quality acceptance required at least 85 seconds of usable samples, measured sample clock within 2% of configured rate, estimated missing-sample fraction no greater than 0.5%, no signed-integer rail samples, at least 25 identifiable pulse cycles, at least 8 valid 10-second windows, stable pulse interval CV no greater than 0.50 when estimable, and no PPG stream errors. The report includes amplitude range, missing-sample and pulse details in `results.json` and each session's `metadata.json`.

## Comparisons

| Comparison | PaPaGei similarity (mean; min–max) | Baseline distance | Signal quality |
|---|---:|---:|---|
| WEARER_1 → WEARER_1 (rewear) | 0.9826 (0.9648–0.9993) | 0.0019 | enroll GOOD, test GOOD; 8+9 PaPaGei windows |
| WEARER_1 → WEARER_2 | 0.4022 (0.0869–0.6382) | 0.0086 | enroll GOOD, test GOOD; 8+9 PaPaGei windows |
| WEARER_1 → WEARER_1 (second rewear) | 0.9835 (0.8580–0.9998) | 0.0027 | enroll GOOD, test GOOD; 8+9 PaPaGei windows |

Genuine comparisons rank above the impostor by PaPaGei: **True**. Genuine comparisons rank as closer than the impostor by the handcrafted baseline: **True**. These are descriptive scores for one enrolled identity, one impostor, and three test sessions. They do not estimate false-accept or false-reject rates or establish a secure authentication operating point. The existing verifier remains fail-closed.

## Machine-readable detail

See `results.json` and the per-session `metadata.json` files for full metrics, including raw amplitude, packet jitter, pulse cycle counts, and exact pairwise score ranges.
