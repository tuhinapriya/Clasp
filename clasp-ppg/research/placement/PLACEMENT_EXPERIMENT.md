# Polar PPG placement sensitivity experiment

Run date: 14 November 2026. Four separate WEARER_1 sessions: normal enrollment, tighter, looser, and repositioned. The 0.80 similarity threshold was unchanged and remains provisional. No training or fine-tuning was performed.

## Live BLE diagnostics

Each mode used a fresh connection and a 10-second callback-counting interval. BLE remained connected and the SDK reported no stream errors or callback errors.

| Mode | PMD notifications | PPG packets / samples | PPG device-clock rate | ACC packets / samples | ACC device-clock rate |
|---|---:|---:|---:|---:|---:|
| PPG only, 135 Hz | 26 | 26 / 1,315 | 135.113 Hz | 0 / 0 | — |
| ACC only, 52 Hz | 5 | 0 / 0 | — | 5 / 467 | 51.793 Hz |
| PPG + ACC | 31 | 26 / 1,307 | 135.120 Hz | 5 / 458 | 51.791 Hz |

Device-advertised PPG rates: 28, 44, 55, 135, 176 Hz. ACC rates: 26, 52, 104, 208, 416 Hz. Simultaneous streaming delivered both callbacks at their requested rates.

## Placement scores

| WEARER_1 placement | Quality-approved windows | PaPaGei cosine scores | Mean | Windows ≥0.80 | Windows above impostor maximum |
|---|---:|---|---:|---:|---:|
| Normal (enrollment) | 2/3 | 0.8979, 0.8979 | 0.8979 | 2/2 | 0/2 |
| Tighter | 3/3 | 0.7791, 0.7986, 0.9313 | 0.8363 | 1/3 | 1/3 |
| Looser | 3/3 | 0.6106, 0.3309, 0.6531 | 0.5315 | 0/3 | 0/3 |
| Repositioned | 3/3 | 0.6264, 0.6218, 0.3336 | 0.5273 | 0/3 | 0/3 |

The previous PPG-only WEARER_2 session produced nine non-overlapping-window similarities with mean 0.1251 and range −0.1554 to 0.9166. Its highest window score overlaps the normal and tighter genuine scores. Only one of eleven genuine placement windows exceeded that impostor maximum. The impostor recording has no ACC data, so it has no motion-quality assessment. Its adjacent windows are not independent trials. The normal session is also its own enrollment, not an independent genuine test.

The placement results do not demonstrate consistent genuine-over-impostor separation. Tighter placement has a mean above 0.80 but only one of three windows meets the cutoff. The two other rewear conditions have no windows above 0.80. These are descriptive results from one wearer and one historical impostor session, not production validation.

The normal placement's third window was rejected for excessive motion (84.74 mg dynamic RMS against a fixed experimental limit of 75 mg). All other reported windows passed the documented PPG and ACC quality gates. Per-window PPG/ACC timestamp residual P95 was 18.2–18.5 ms.

Detailed, pseudonymous scores and per-window quality metrics are in `placement_results.json`. Raw sensor recordings and device identifiers remain local and are excluded from Git.
