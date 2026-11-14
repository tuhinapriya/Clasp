# PaPaGei-P evaluation on Polar Verity Sense captures

## Result

I ran the official frozen PaPaGei-P checkpoint on the three local, real Polar Verity Sense recordings. Cosine similarity is shown below; larger means embeddings are more similar.

| Comparison | Window pairs | Mean cosine | Pairwise range |
|---|---:|---:|---:|
| Participant 1 baseline ↔ participant 1, after sensor removal and reattachment | 2 | **0.6927** | 0.6651–0.7204 |
| Participant 1 baseline ↔ participant 2 | 2 | **0.8509** | 0.8457–0.8561 |
| Participant 1 rewear ↔ participant 2 | 4 | **0.9476** | 0.9254–0.9684 |

The genuine rewear comparison scores lower than both different-person comparisons. On these recordings PaPaGei-P therefore does not distinguish the enrolled person from the other participant. No decision threshold was fitted, and the production verifier was not changed. Verification remains fail-closed.

There is only one participant-1 baseline window because that capture lasted 19.83 seconds. The rewear and participant-2 captures each yield two windows. Window-pair scores are correlated observations from only three short sessions, not independent trials. This is a feasibility result, not an estimate of false-accept/false-reject rates or evidence of identity verification.

## Model and availability check

The official [PaPaGei repository](https://github.com/Nokia-Bell-Labs/papagei-foundation-model) contains model code, extraction examples, and links to the [Zenodo checkpoint record](https://zenodo.org/records/13983110). `papagei_p.pt` downloaded from that record passed its published MD5 check (`052b50807465fae61e08e2b7acbb5c53`). The local checkpoint SHA-256 is recorded in the ignored JSON results file. The official source was pinned to commit `0c537dad4d2850e15b724260de820dd68d77f0b0` for this run.

PaPaGei-P is the best first PaPaGei candidate for this question: its participant-aware contrastive training uses different segments from the same participant as positive pairs. It is still a general PPG representation model, not a validated identity-authentication system, and it was not pretrained specifically for the upper-arm Verity Sense sensor. The official release describes 10-second PPG windows at 125 Hz, z-score normalization, order-4 Chebyshev-II filtering from 0.5–12 Hz, and pyPPG's 50 ms PPG smoothing. The input tensor shape is `[batch, 1, 1250]`.

## Evaluation procedure

- Source files were the existing locally stored real captures documented in `PPG_FINDINGS.md`; no synthetic waveform was used.
- All sessions use optical channel `ppg_3`, selected from the previous capture review and held constant across sessions.
- At the measured source rate of 135 Hz: z-score each recording, apply the official pyPPG-equivalent Chebyshev-II filter and 50 ms smoothing, segment into non-overlapping complete 10-second windows, then resample each to 1250 samples at 125 Hz.
- Incomplete tails are discarded rather than padded. Resulting window counts are baseline 1, participant 2 has 2, and rewear has 2.
- The frozen official `ResNet1D` returns `(out, out_emb)`. The official feature-extraction example selects `output_idx=0`; that 512-D contrastive output was L2-normalized and compared with cosine similarity.
- No calibration, threshold selection, or fitting was performed on these sessions.

The reproducible runner is `scripts/evaluate_papagei.py`; it writes `data/papagei_results/papagei_p_scores.json` (ignored local data). Checkpoint and venv files under `weights/` and `.venv-papagei/` are also git-ignored.

### Run it again

Use Python 3.10 per the official setup guidance. Install PyTorch, NumPy, SciPy, pandas, dotmap, scikit-learn, matplotlib, and tqdm in a separate environment; install `pyPPG==1.0.41` without forcing its historical pinned dependency versions if those conflict with the current Python/SciPy wheels. The runner uses the official pyPPG filter behavior directly via SciPy and imports `ResNet1D` from the official source clone.

```sh
git clone https://github.com/Nokia-Bell-Labs/papagei-foundation-model /tmp/papagei-official
mkdir -p clasp-ppg/weights
curl -L https://zenodo.org/api/records/13983110/files/papagei_p.pt/content \
  -o clasp-ppg/weights/papagei_p.pt
md5 clasp-ppg/weights/papagei_p.pt
OMP_NUM_THREADS=1 python clasp-ppg/scripts/evaluate_papagei.py \
  --source /tmp/papagei-official
```

The MD5 output should be `052b50807465fae61e08e2b7acbb5c53`.

## Other public biometric approaches checked

- **Seeing Red (Oxford):** the [official implementation](https://github.com/ssloxford/seeing-red) and paper are available. It uses smartphone-camera fingertip video (frame LUMA at about camera frame rate), custom pulse segmentation, and engineered features with conventional classifiers; the repo does not provide a compatible pretrained neural embedding checkpoint. It is useful prior art for cross-session PPG biometrics, but its acquisition modality and sampling differ materially from the Verity Sense optical stream, so it cannot be applied as a frozen model here.
- **MTL-RAPID / ANT_PPG:** a 2025 smartwatch-authentication paper describes a task-specific PPG model and reports its code/model and dataset as open. The public [ANT_PPG repository](https://github.com/thuhci/ANT_PPG) currently provides a README and release agreement; it documents a downloadable 2,000-pair test set and access-by-application for the full training data, but does not expose the model implementation or checkpoint files in the repository. I could not verify an official, downloadable pretrained artifact that can be run on these recordings. Its reported benchmark is not evidence of performance on Polar data.

The Oxford approach provides reusable research code but no appropriate pretrained model. The MTL-RAPID lead is closer to wearable authentication, but its claimed reusable artifact was not verifiable from the public release checked. Neither currently supports a credible frozen-model evaluation on these captures. The next defensible step is to locate an official MTL-RAPID checkpoint/code release or collect a larger, session-separated dataset, then pre-register an evaluation with enrollment/development and held-out test sessions. Training or fine-tuning should wait until the model/data provenance and a non-leaking protocol are settled.
