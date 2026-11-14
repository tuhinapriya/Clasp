"""Exploratory pulse morphology and second-derivative shape, without fitted cutoffs."""
import numpy as np
from datetime import datetime, timezone
from .biometric import _waveforms, _distance
from .models import Capture, PPGFrame
from .quality import assess_quality
from .storage import capture_matrix


def pulse_templates(capture):
    quality = assess_quality(capture_matrix(capture), capture.sampling_rate_hz)
    beats = _waveforms(capture, quality)
    if len(beats) < 8:
        return None, None
    morphology = np.median(beats, axis=0)
    norm = np.linalg.norm(morphology)
    if not np.isfinite(norm) or norm < 1e-9:
        return None, None
    morphology = morphology / norm
    apg = np.gradient(np.gradient(morphology))
    apg -= np.mean(apg)
    apg_norm = np.linalg.norm(apg)
    return morphology, apg / apg_norm if np.isfinite(apg_norm) and apg_norm > 1e-9 else None


def window_templates(ppg, rate):
    now = datetime.now(timezone.utc)
    capture = Capture('primary-user', 'window', 'verification', now, sampling_rate_hz=rate)
    capture.frames.append(PPGFrame(round(len(ppg) * 1e9 / rate), now, tuple(map(tuple, ppg))))
    return pulse_templates(capture)


def distance(a, b):
    if a is None or b is None:
        return None
    return _distance(np.asarray(a), np.asarray(b))
