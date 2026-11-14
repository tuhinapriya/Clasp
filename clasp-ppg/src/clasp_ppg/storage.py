from __future__ import annotations

import csv
import json
import re
from datetime import datetime
from dataclasses import asdict
from datetime import timezone
from pathlib import Path

import numpy as np

from .models import Capture, PPGFrame


def capture_matrix(capture: Capture) -> np.ndarray:
    rows = [row for frame in capture.frames for row in frame.samples]
    if not rows:
        return np.empty((0, 4), dtype=float)
    width = max(map(len, rows))
    return np.asarray([list(row) + [np.nan] * (width - len(row)) for row in rows])


def save_capture(capture: Capture, root: str | Path) -> Path:
    """Write raw packet and per-sample CSVs plus metadata JSON."""
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", capture.participant_id):
        raise ValueError("participant_id must be 1-64 letters, digits, underscores, or hyphens")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", capture.session_id):
        raise ValueError("session_id must be 1-64 letters, digits, underscores, or hyphens")
    folder = Path(root) / capture.participant_id / capture.session_id
    folder.mkdir(parents=True, exist_ok=True)
    sample_path = folder / "ppg_samples.csv"
    names = list(capture.channel_names)
    matrix = capture_matrix(capture)
    if matrix.shape[1] > len(names):
        names.extend(f"channel_{i + 1}" for i in range(len(names), matrix.shape[1]))
    with sample_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["sample_index", "sensor_timestamp_ns", *names[: matrix.shape[1]]])
        offset = 0
        for frame in capture.frames:
            n = len(frame.samples)
            # PMD frame timestamp is the timestamp of its final sample.
            first_ns = frame.sensor_timestamp_ns - round((n - 1) * 1e9 / capture.sampling_rate_hz)
            for i, row in enumerate(frame.samples):
                writer.writerow([offset + i, first_ns + round(i * 1e9 / capture.sampling_rate_hz), *row])
            offset += n
    meta = asdict(capture)
    meta["started_at"] = capture.started_at.astimezone(timezone.utc).isoformat()
    meta["ended_at"] = capture.ended_at.astimezone(timezone.utc).isoformat() if capture.ended_at else None
    meta.pop("frames")
    meta["raw_file"] = sample_path.name
    from .quality import assess_quality

    meta["quality"] = asdict(assess_quality(matrix, capture.sampling_rate_hz))
    meta["timestamp_note"] = "sensor_timestamp_ns is the PMD device clock; sample times within each packet are estimated using the configured nominal sample rate"
    (folder / "metadata.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return folder


def load_capture(folder: str | Path) -> Capture:
    """Load a saved capture for offline analysis; does not fabricate missing rows."""
    folder = Path(folder)
    meta = json.loads((folder / "metadata.json").read_text(encoding="utf-8"))
    rows = []
    stamps = []
    with (folder / meta.get("raw_file", "ppg_samples.csv")).open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            stamps.append(int(row["sensor_timestamp_ns"]))
            rows.append(tuple(int(row[name]) for name in meta["channel_names"]))
    capture = Capture(
        participant_id=meta["participant_id"],
        session_id=meta["session_id"],
        session_type=meta["session_type"],
        started_at=datetime.fromisoformat(meta["started_at"]),
        ended_at=datetime.fromisoformat(meta["ended_at"]) if meta.get("ended_at") else None,
        sampling_rate_hz=int(meta["sampling_rate_hz"]),
        channel_names=tuple(meta["channel_names"]),
        device_name=meta.get("device_name"),
        device_address=meta.get("device_address"),
        stream_errors=meta.get("stream_errors", {}),
    )
    if rows:
        capture.frames.append(PPGFrame(stamps[-1], capture.started_at, tuple(rows)))
    return capture
