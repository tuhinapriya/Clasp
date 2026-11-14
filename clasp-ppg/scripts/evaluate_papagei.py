#!/usr/bin/env python3
"""Frozen PaPaGei-P embedding evaluation on locally recorded Polar sessions.

Requires the official PaPaGei repository clone and official papagei_p.pt weights.
No thresholds are fitted. Incomplete trailing windows are discarded.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
from pathlib import Path
import sys

import numpy as np
import torch
from scipy.signal import cheby2, filtfilt, resample_poly

ROOT = Path(__file__).resolve().parents[1]
WINDOW_SECONDS = 10
SOURCE_FS = 135
MODEL_FS = 125
CHANNEL = "ppg_3"

SESSIONS = {
    "p1_baseline": ("participant_1/df4b4e82e3a2470685b22c6c22bc822b/ppg_samples.csv", "participant_1"),
    "p2_impostor": ("participant_2/0aca17ca431046518030d944b59e70f2/ppg_samples.csv", "participant_2"),
    "p1_rewear": ("participant_1/cbdba890fec040a8bab8746513599162/ppg_samples.csv", "participant_1"),
}


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def load_windows(path: Path) -> tuple[np.ndarray, int]:
    with path.open(newline="") as f:
        rows = list(csv.DictReader(f))
    x = np.asarray([float(row[CHANNEL]) for row in rows], dtype=np.float64)
    # Official pipeline: z-score, 4th-order Chebyshev-II 0.5-12 Hz at 20 dB,
    # pyPPG's 50 ms moving-average smoothing at fs >= 75 Hz, then resample.
    std = x.std()
    if not np.isfinite(std) or std == 0:
        raise ValueError(f"Invalid/flat signal: {path}")
    x = (x - x.mean()) / std
    b, a = cheby2(4, 20, [0.5, 12], btype="bandpass", fs=SOURCE_FS)
    x = filtfilt(b, a, x)
    win = round(SOURCE_FS * 50 / 1000)
    x = filtfilt(np.ones(win) / win, 1, x)
    n = len(x) // (WINDOW_SECONDS * SOURCE_FS)
    windows = []
    for i in range(n):
        segment = x[i * WINDOW_SECONDS * SOURCE_FS:(i + 1) * WINDOW_SECONDS * SOURCE_FS]
        segment = resample_poly(segment, 25, 27)  # 135 -> 125 Hz
        if len(segment) != 1250:
            raise AssertionError(f"Expected 1250 samples, got {len(segment)}")
        windows.append(segment.astype(np.float32))
    if not windows:
        raise ValueError(f"No complete 10 second windows in {path} ({len(x)/SOURCE_FS:.2f}s)")
    return np.stack(windows), len(rows)


def load_model(source: Path, checkpoint: Path) -> torch.nn.Module:
    sys.path.insert(0, str(source))
    from models.resnet import ResNet1D

    model = ResNet1D(in_channels=1, base_filters=32, kernel_size=3, stride=2,
                     groups=1, n_block=18, n_classes=512)
    state = torch.load(checkpoint, map_location="cpu", weights_only=True)
    if isinstance(state, dict) and "state_dict" in state:
        state = state["state_dict"]
    state = {k.removeprefix("module."): v for k, v in state.items()}
    model.load_state_dict(state, strict=True)
    model.eval()
    return model


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True, help="Path to official papagei-foundation-model clone")
    parser.add_argument("--checkpoint", type=Path, default=ROOT / "weights/papagei_p.pt")
    parser.add_argument("--data", type=Path, default=ROOT / "data")
    parser.add_argument("--output", type=Path, default=ROOT / "data/papagei_results")
    args = parser.parse_args()

    model = load_model(args.source, args.checkpoint)
    embedded = {}
    session_info = {}
    with torch.inference_mode():
        for name, (relpath, person) in SESSIONS.items():
            windows, sample_count = load_windows(args.data / relpath)
            x = torch.from_numpy(windows[:, None, :])
            # Official PaPaGei feature extractor uses output_idx=0.
            vec = model(x)[0].cpu().numpy()
            vec /= np.maximum(np.linalg.norm(vec, axis=1, keepdims=True), 1e-12)
            embedded[name] = vec
            session_info[name] = {"participant": person, "samples": sample_count,
                                  "complete_10s_windows": len(windows), "discarded_tail_seconds": round(sample_count / SOURCE_FS - len(windows) * 10, 4)}

    pairs = [
        ("p1_baseline", "p1_rewear", "same_person_sensor_removed_reattached"),
        ("p1_baseline", "p2_impostor", "different_people"),
        ("p1_rewear", "p2_impostor", "different_people"),
        ("p1_baseline", "p1_baseline", "same_session_window_repeatability"),
    ]
    comparisons = []
    for a, b, label in pairs:
        scores = embedded[a] @ embedded[b].T
        comparisons.append({"session_a": a, "session_b": b, "comparison": label,
                            "window_pairs": int(scores.size), "mean_cosine": float(scores.mean()),
                            "min_cosine": float(scores.min()), "max_cosine": float(scores.max()),
                            "scores": scores.round(8).tolist()})
    args.output.mkdir(parents=True, exist_ok=True)
    result = {
        "model": "PaPaGei-P (official participant-aware checkpoint), frozen; output_idx=0",
        "source_repo": "https://github.com/Nokia-Bell-Labs/papagei-foundation-model",
        "source_commit": "0c537dad4d2850e15b724260de820dd68d77f0b0",
        "checkpoint": "papagei_p.pt",
        "checkpoint_sha256": sha256(args.checkpoint),
        "preprocessing": {"source_fs_hz": SOURCE_FS, "channel": CHANNEL,
                          "zscore_per_recording": True, "chebyshev_ii_order": 4,
                          "chebyshev_ii_ripple_db": 20, "bandpass_hz": [0.5, 12],
                          "pyppg_moving_average_ms": 50, "model_fs_hz": MODEL_FS,
                          "window_seconds": WINDOW_SECONDS, "window_samples": 1250,
                          "windowing": "non-overlapping complete windows; incomplete trailing data discarded"},
        "sessions": session_info,
        "comparisons": comparisons,
        "threshold_fit": False,
        "decision": "research scores only; verifier behavior unchanged and fail-closed",
    }
    (args.output / "papagei_p_scores.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
