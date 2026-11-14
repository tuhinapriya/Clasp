from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from scipy.signal import butter, find_peaks, resample, sosfiltfilt

from .models import Capture
from .quality import QualityReport, assess_quality
from .storage import capture_matrix


@dataclass(frozen=True)
class VerificationResult:
    subject_id: str
    decision: str
    score: float | None
    threshold: float | None
    signal_quality: str
    beat_count: int
    reason: str


class EnrollmentStore:
    """Local JSON templates. Store these as biometric data and keep them private."""

    def __init__(self, directory: str | Path) -> None:
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)

    def path_for(self, subject_id: str) -> Path:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", subject_id):
            raise ValueError("subject_id must be 1-64 letters, digits, underscores, or hyphens")
        return self.directory / f"{subject_id}.json"

    def load(self, subject_id: str) -> dict:
        return json.loads(self.path_for(subject_id).read_text(encoding="utf-8"))

    def save(self, subject_id: str, payload: dict) -> None:
        path = self.path_for(subject_id)
        temp = path.with_suffix(".json.tmp")
        temp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        temp.replace(path)


def _waveforms(capture: Capture, quality: QualityReport) -> np.ndarray:
    matrix = capture_matrix(capture)
    if not quality.usable or quality.selected_channel is None:
        return np.empty((0, 64))
    signal = matrix[:, quality.selected_channel]
    signal = signal[np.isfinite(signal)]
    if len(signal) < capture.sampling_rate_hz * 8:
        return np.empty((0, 64))
    sos = butter(3, (0.5, 5.0), btype="bandpass", fs=capture.sampling_rate_hz, output="sos")
    try:
        filtered = sosfiltfilt(sos, signal)
    except ValueError:
        return np.empty((0, 64))
    # Select the polarity with larger robust peak prominence; Verity optical
    # channels can have either polarity depending on sensor/firmware behavior.
    distance = max(1, int(capture.sampling_rate_hz * 0.35))
    p_hi, _ = find_peaks(filtered, distance=distance, prominence=np.std(filtered) * 0.15)
    p_lo, _ = find_peaks(-filtered, distance=distance, prominence=np.std(filtered) * 0.15)
    if len(p_lo) >= 3 and len(p_hi) >= 3:
        polarity = 1 if np.median(filtered[p_hi]) >= np.median(-filtered[p_lo]) else -1
    else:
        polarity = 1 if len(p_hi) >= len(p_lo) else -1
    peaks, _ = find_peaks(polarity * filtered, distance=distance, prominence=np.std(filtered) * 0.15)
    beats: list[np.ndarray] = []
    for left, right in zip(peaks[:-1], peaks[1:]):
        period = right - left
        if period < capture.sampling_rate_hz * 0.35 or period > capture.sampling_rate_hz * 1.8:
            continue
        beat = resample(filtered[left:right], 64)
        beat = beat - np.median(beat)
        norm = np.linalg.norm(beat)
        if norm > 1e-9:
            beats.append(beat / norm)
    return np.asarray(beats) if beats else np.empty((0, 64))


def enroll(subject_id: str, captures: Capture | list[Capture], store: EnrollmentStore) -> dict:
    """Create an experimental morphology template from real recorded captures."""
    if isinstance(captures, Capture):
        captures = [captures]
    waves = []
    sessions = []
    for capture in captures:
        if capture.participant_id != subject_id:
            raise ValueError("capture participant_id must match subject_id")
        quality = assess_quality(capture_matrix(capture), capture.sampling_rate_hz)
        beats = _waveforms(capture, quality)
        if len(beats):
            waves.extend(beats)
            sessions.append(capture.session_id)
    if len(waves) < 20:
        raise ValueError(f"enrollment needs at least 20 usable pulse cycles; got {len(waves)}")
    beat_array = np.asarray(waves)
    template = np.median(beat_array, axis=0)
    template /= max(np.linalg.norm(template), 1e-9)
    payload = {
        "subject_id": subject_id,
        "method": "bandpass_0.5_5hz_normalized_64_point_pulse_morphology_v1",
        "sampling_rates_hz": sorted({c.sampling_rate_hz for c in captures}),
        "sessions": sessions,
        "beat_count": len(beat_array),
        "template": template.tolist(),
        "threshold": None,
        "calibration": {"status": "UNCALIBRATED", "threshold": None},
        "research_note": "One-class morphology template only. Verification stays UNCERTAIN until held-out genuine and impostor sessions establish separation.",
    }
    store.save(subject_id, payload)
    return payload


def _distance(a: np.ndarray, b: np.ndarray) -> float:
    # Cosine distance after pulse normalization; bounded [0, 2].
    return float(1.0 - np.clip(np.dot(a, b), -1.0, 1.0))


def _capture_score(capture: Capture, template: np.ndarray) -> tuple[float | None, QualityReport, int]:
    quality = assess_quality(capture_matrix(capture), capture.sampling_rate_hz)
    if not quality.usable:
        return None, quality, 0
    beats = _waveforms(capture, quality)
    if len(beats) < 8:
        return None, quality, len(beats)
    test = np.median(beats, axis=0)
    test /= max(np.linalg.norm(test), 1e-9)
    return _distance(test, template), quality, len(beats)


def calibrate(
    subject_id: str,
    genuine_captures: list[Capture],
    impostor_captures: list[Capture],
    store: EnrollmentStore,
) -> dict:
    """Set an operating point only if held-out genuine/impostor scores separate."""
    profile = store.load(subject_id)
    enrolled_sessions = set(profile["sessions"])
    if not genuine_captures or not impostor_captures:
        raise ValueError("calibration requires at least one held-out genuine and impostor capture")
    if any(c.participant_id != subject_id for c in genuine_captures):
        raise ValueError("genuine capture participant_id must match subject_id")
    if any(c.participant_id == subject_id for c in impostor_captures):
        raise ValueError("impostor captures must have a different participant_id")
    if any(c.session_id in enrolled_sessions for c in [*genuine_captures, *impostor_captures]):
        raise ValueError("calibration sessions must be held out from enrollment")

    template = np.asarray(profile["template"], dtype=float)
    genuine_scores = []
    impostor_scores = []
    for capture, destination in [
        *((c, genuine_scores) for c in genuine_captures),
        *((c, impostor_scores) for c in impostor_captures),
    ]:
        score, _quality, _beats = _capture_score(capture, template)
        if score is not None:
            destination.append(score)
    if not genuine_scores or not impostor_scores:
        raise ValueError("calibration sessions did not contain enough usable pulse cycles")

    genuine_max = float(max(genuine_scores))
    impostor_min = float(min(impostor_scores))
    separated = genuine_max < impostor_min
    threshold = (genuine_max + impostor_min) / 2 if separated else None
    calibration = {
        "status": "CALIBRATED" if separated else "NO_SEPARATION",
        "threshold": threshold,
        "genuine_scores": genuine_scores,
        "impostor_scores": impostor_scores,
        "genuine_max": genuine_max,
        "impostor_min": impostor_min,
        "held_out_sessions": [c.session_id for c in [*genuine_captures, *impostor_captures]],
        "note": "Small-sample calibration is exploratory and does not establish error rates.",
    }
    profile["threshold"] = threshold
    profile["calibration"] = calibration
    store.save(subject_id, profile)
    return calibration


def verify(subject_id: str, capture: Capture, store: EnrollmentStore) -> VerificationResult:
    try:
        profile = store.load(subject_id)
    except FileNotFoundError:
        return VerificationResult(subject_id, "UNCERTAIN", None, None, "NO_DATA", 0, "no_enrollment_template")
    matrix = capture_matrix(capture)
    quality = assess_quality(matrix, capture.sampling_rate_hz)
    if not quality.usable:
        return VerificationResult(subject_id, "UNCERTAIN", None, profile.get("threshold"), quality.label, 0, ",".join(quality.reasons))
    beats = _waveforms(capture, quality)
    if len(beats) < 8:
        return VerificationResult(subject_id, "UNCERTAIN", None, profile.get("threshold"), quality.label, len(beats), "too_few_clean_pulse_cycles")
    test = np.median(beats, axis=0)
    test /= max(np.linalg.norm(test), 1e-9)
    score = _distance(test, np.asarray(profile["template"], dtype=float))
    calibration = profile.get("calibration", {})
    threshold = calibration.get("threshold")
    if calibration.get("status") != "CALIBRATED" or threshold is None:
        reason = "calibration_required" if calibration.get("status") == "UNCALIBRATED" else "no_genuine_impostor_separation"
        return VerificationResult(subject_id, "UNCERTAIN", score, None, quality.label, len(beats), reason)
    threshold = float(threshold)
    return VerificationResult(
        subject_id,
        "MATCH" if score <= threshold else "MISMATCH",
        score,
        threshold,
        quality.label,
        len(beats),
        "morphology_distance_within_threshold" if score <= threshold else "morphology_distance_exceeds_threshold",
    )


def evaluate_labeled(captures: list[Capture]) -> dict:
    """Pairwise genuine/impostor distances; label rewear sessions explicitly."""
    feature_rows = []
    for c in captures:
        q = assess_quality(capture_matrix(c), c.sampling_rate_hz)
        beats = _waveforms(c, q)
        if len(beats) >= 8:
            template = np.median(beats, axis=0)
            template /= max(np.linalg.norm(template), 1e-9)
            feature_rows.append((c, template, len(beats)))
    pairs = []
    for i, (a, fa, na) in enumerate(feature_rows):
        for b, fb, nb in feature_rows[i + 1 :]:
            same = a.participant_id == b.participant_id
            pairs.append({
                "session_a": a.session_id,
                "session_b": b.session_id,
                "participant_a": a.participant_id,
                "participant_b": b.participant_id,
                "genuine": same,
                "rewear_pair": same and "rewear" in {a.session_type.lower(), b.session_type.lower()},
                "score": _distance(fa, fb),
                "beats_a": na,
                "beats_b": nb,
            })
    genuine = [p["score"] for p in pairs if p["genuine"]]
    impostor = [p["score"] for p in pairs if not p["genuine"]]
    return {
        "sessions_used": len(feature_rows),
        "pair_count": len(pairs),
        "pairs": pairs,
        "genuine_scores": genuine,
        "impostor_scores": impostor,
        "rewear_pairs": sum(p["rewear_pair"] for p in pairs),
        "note": "Descriptive feasibility results; adjacent windows/sessions are not independent trials and this does not establish FAR/FRR.",
    }
