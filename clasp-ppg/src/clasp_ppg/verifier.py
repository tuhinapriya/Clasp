"""Experimental three-window live verifier. It is not an authorization gateway."""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from scipy.signal import cheby2, filtfilt, resample_poly

from .motion_quality import evaluate_window, split_synchronized_windows


DEFAULT_PROVISIONAL_THRESHOLD = 0.80


class PaPaGeiEmbedder:
    """Frozen official PaPaGei-P ResNet embedding adapter."""
    def __init__(self, source: str | Path, checkpoint: str | Path):
        import torch
        sys.path.insert(0, str(Path(source)))
        from models.resnet import ResNet1D
        self.torch = torch
        self.model = ResNet1D(in_channels=1, base_filters=32, kernel_size=3,
                              stride=2, groups=1, n_block=18, n_classes=512)
        state = torch.load(checkpoint, map_location="cpu", weights_only=True)
        if isinstance(state, dict) and "state_dict" in state: state = state["state_dict"]
        self.model.load_state_dict({k.removeprefix("module."): v for k, v in state.items()}, strict=True)
        self.model.eval()

    def __call__(self, samples: np.ndarray, sampling_rate_hz: int) -> np.ndarray:
        channel = samples[:, 2] if samples.shape[1] > 2 else samples[:, 0]
        x = (channel.astype(np.float64) - np.mean(channel)) / max(float(np.std(channel)), 1e-12)
        b, a = cheby2(4, 20, [0.5, 12], btype="bandpass", fs=sampling_rate_hz)
        x = filtfilt(b, a, x)
        if sampling_rate_hz >= 75:
            n = max(1, round(sampling_rate_hz * .050))
            x = filtfilt(np.ones(n) / n, 1, x)
        from fractions import Fraction
        ratio = Fraction(125 / sampling_rate_hz).limit_denominator(1000)
        x = resample_poly(x, ratio.numerator, ratio.denominator)
        if len(x) < 1250 or not np.isfinite(x).all(): raise ValueError("PaPaGei requires one finite 10-second window")
        # Resample_poly returns the ceiling length for some rational ratios
        # (e.g. 1351 raw samples -> 1251). The frozen model uses exactly 1250.
        x = x[:1250]
        with self.torch.inference_mode():
            vec = self.model(self.torch.from_numpy(x.astype(np.float32)[None, None, :]))[0].cpu().numpy()
        vec = np.asarray(vec).reshape(-1)
        return vec / max(float(np.linalg.norm(vec)), 1e-12)


@dataclass
class EnrollmentProfile:
    embeddings: list[list[float]]
    source_session_id: str
    morphology_template: list[float] | None = None
    apg_template: list[float] | None = None

    @property
    def prototype(self):
        vec = np.mean(np.asarray(self.embeddings), axis=0)
        return vec / max(float(np.linalg.norm(vec)), 1e-12)


@dataclass
class ActiveVerificationSession:
    active: bool = True
    invalidation_reason: str | None = None
    monitor_task: object | None = None
    def invalidate(self, reason: str):
        self.active = False
        self.invalidation_reason = reason


def embed_approved_windows(capture, embedder, count=3):
    windows = split_synchronized_windows(capture, count=count)
    entries = []
    for index, (ppg, pt, acc, at) in enumerate(windows):
        quality = evaluate_window(ppg, pt, acc, at, capture.sampling_rate_hz,
                                  capture.accelerometer_sampling_rate_hz)
        score = None
        if quality.usable:
            try: score = embedder(ppg, capture.sampling_rate_hz)
            except Exception as exc:
                quality = type(quality)(False, quality.metrics, (*quality.rejection_reasons, f"embedding_failed:{type(exc).__name__}"))
        entries.append({"window_index": index + 1, "quality_approved": quality.usable,
                        "quality": quality.metrics, "rejection_reasons": list(quality.rejection_reasons),
                        "embedding": score})
    return entries


def enroll_capture(capture, embedder, count=3) -> EnrollmentProfile:
    entries = embed_approved_windows(capture, embedder, count=count)
    vectors = [e["embedding"] for e in entries if e["embedding"] is not None]
    if len(vectors) < 2: raise ValueError("enrollment requires at least two quality-approved windows")
    return EnrollmentProfile([np.asarray(v).tolist() for v in vectors], capture.session_id)


def decide_windows(entries, profile: EnrollmentProfile, threshold: float = DEFAULT_PROVISIONAL_THRESHOLD):
    if not 0 <= threshold <= 1: raise ValueError("threshold must be between 0 and 1")
    tested = []
    for entry in entries:
        row = dict(entry)
        emb = row.pop("embedding", None)
        row["similarity"] = float(np.dot(profile.prototype, emb)) if row["quality_approved"] and emb is not None else None
        row["exceeds_provisional_threshold"] = row["similarity"] is not None and row["similarity"] >= threshold
        tested.append(row)
    clean = [x for x in tested if x["quality_approved"] and x["similarity"] is not None]
    above = sum(x["exceeds_provisional_threshold"] for x in clean)
    if len(clean) < 2: decision = "UNCERTAIN"
    elif above >= 2: decision = "MATCH"
    elif len(clean) - above >= 2: decision = "MISMATCH"
    else: decision = "UNCERTAIN"
    return {"decision": decision, "threshold": threshold, "threshold_status": "provisional_not_production_calibrated",
            "quality_approved_windows": len(clean), "above_threshold_windows": above,
            "windows": tested, "rejection_reasons": [reason for x in tested for reason in x["rejection_reasons"]]}


async def verify_live(sensor, profile: EnrollmentProfile, embedder, *, participant_id="WEARER_1",
                      threshold=DEFAULT_PROVISIONAL_THRESHOLD, data_dir=None):
    """Capture a live 30s verification; early aborts produce UNCERTAIN."""
    if sensor.status.connected and not sensor.status.streaming:
        await sensor.wait_for_streaming(20.0)
    if not sensor.status.connected or not sensor.status.streaming:
        return {"decision": "UNCERTAIN", "rejection_reasons": ["polar_not_streaming"], "windows": []}
    sensor.disconnect_event.clear()
    capture = await sensor.capture(participant_id, 32, session_type="live_verification", output_dir=data_dir,
                                   abort_event=sensor.disconnect_event)
    if sensor.disconnect_event.is_set() or not sensor.status.connected:
        return {"decision": "UNCERTAIN", "rejection_reasons": ["polar_disconnected"], "windows": []}
    result = decide_windows(embed_approved_windows(capture, embedder), profile, threshold)
    result["session_id"] = capture.session_id
    if result["decision"] == "MATCH":
        handle = ActiveVerificationSession()
        sensor._bind_verification(handle)
        handle.monitor_task = asyncio.create_task(_monitor_active_session(sensor, handle))
        result["active_session"] = handle
    return result


async def _monitor_active_session(sensor, session):
    """Watch for device disconnection or cessation of raw physiological PPG."""
    import asyncio
    from datetime import datetime, timezone
    while session.active:
        if sensor.disconnect_event.is_set() or not sensor.status.connected or not sensor.status.streaming:
            session.invalidate("polar_disconnected")
            break
        last = sensor.status.last_sample_at
        if last is None or (datetime.now(timezone.utc) - last).total_seconds() > .5:
            session.invalidate("physiological_signal_lost")
            break
        if not sensor.status.physiological_signal_present:
            session.invalidate("physiological_signal_lost")
            break
        await asyncio.sleep(.1)
    if not session.active and getattr(sensor, "_active_verification", None) is session:
        sensor._active_verification = None
