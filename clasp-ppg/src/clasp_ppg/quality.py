from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class QualityReport:
    label: str
    usable: bool
    sample_count: int
    duration_s: float
    selected_channel: int | None
    channel_std: tuple[float, ...]
    reasons: tuple[str, ...]


def assess_quality(samples: np.ndarray, sampling_rate_hz: float) -> QualityReport:
    """Conservative signal checks; no skin-contact claim is inferred from PPG."""
    data = np.asarray(samples, dtype=float)
    if data.ndim == 1:
        data = data[:, None]
    if data.ndim != 2:
        raise ValueError("samples must be a 1D or 2D array")
    if sampling_rate_hz <= 0:
        raise ValueError("sampling_rate_hz must be positive")

    n = data.shape[0]
    duration = n / sampling_rate_hz
    if not n:
        return QualityReport("NO_DATA", False, 0, 0.0, None, (), ("no_samples",))

    finite = np.isfinite(data).all(axis=1)
    data = data[finite]
    if not len(data):
        return QualityReport("BAD", False, n, duration, None, (), ("non_finite",))
    stds = np.std(data, axis=0)
    # The fourth Verity Sense channel is ambient light; do not select it.
    optical_channels = min(3, data.shape[1])
    std_optical = stds[:optical_channels]
    channel = int(np.argmax(std_optical)) if optical_channels else None
    reasons: list[str] = []
    if duration < 8:
        reasons.append("capture_too_short")
    if channel is None or std_optical[channel] < 1.0:
        reasons.append("flat_or_missing_optical_signal")
    if n and 1.0 - len(data) / n > 0.01:
        reasons.append("non_finite_samples")
    usable = not reasons
    return QualityReport(
        "GOOD" if usable else "BAD",
        usable,
        len(data),
        duration,
        channel,
        tuple(float(v) for v in stds),
        tuple(reasons),
    )
