"""Experimental, conservative PPG and motion quality checks for live windows."""
from __future__ import annotations

from dataclasses import asdict, dataclass

import numpy as np
from scipy.signal import butter, find_peaks, sosfiltfilt

from .models import Capture


@dataclass(frozen=True)
class WindowQuality:
    usable: bool
    metrics: dict
    rejection_reasons: tuple[str, ...]


def _samples_with_times(capture: Capture, acc: bool = False):
    frames = capture.acc_frames if acc else capture.frames
    rate = capture.accelerometer_sampling_rate_hz if acc else capture.sampling_rate_hz
    rows, times = [], []
    previous = None
    for frame in frames:
        values = frame.samples
        if not values:
            continue
        start = frame.sensor_timestamp_ns - round((len(values) - 1) * 1e9 / rate)
        if previous is not None and frame.sensor_timestamp_ns <= previous:
            raise ValueError('Non-monotonic Polar packet timestamps')
        for i, value in enumerate(values):
            stamp = start + round(i * 1e9 / rate)
            if previous is None or stamp > previous:
                rows.append(value)
                times.append(stamp)
        previous = frame.sensor_timestamp_ns
    return np.asarray(rows, dtype=float), np.asarray(times, dtype=np.int64)


def _packet_clock(frames, nominal_rate):
    ordered = sorted((f for f in frames if f.samples), key=lambda f: f.sensor_timestamp_ns)
    rates, missing = [], 0
    for left, right in zip(ordered, ordered[1:]):
        delta = (right.sensor_timestamp_ns - left.sensor_timestamp_ns) / 1e9
        if delta <= 0: continue
        count = len(left.samples)
        rates.append(count / delta)
        expected = round(delta * nominal_rate)
        if expected > count + max(2, round(count * .1)):
            missing += expected - count
    measured = float(np.median(rates)) if rates else None
    return {"nominal_rate_hz": nominal_rate, "observed_rate_hz": measured,
            "inter_packet_missing_samples_estimate": int(missing),
            "packet_count": len(ordered)}


def split_synchronized_windows(capture: Capture, count: int = 3, seconds: float = 10.0):
    """Return non-overlapping PPG/ACC windows on the Polar device timebase."""
    ppg, pt = _samples_with_times(capture)
    acc, at = _samples_with_times(capture, True)
    if not len(ppg) or not len(acc):
        return []
    start = max(pt[0], at[0])
    windows = []
    for index in range(count):
        lo = start + int(index * seconds * 1e9)
        hi = lo + int(seconds * 1e9)
        pmask, amask = (pt >= lo) & (pt < hi), (at >= lo) & (at < hi)
        windows.append((ppg[pmask], pt[pmask], acc[amask], at[amask]))
    return windows


def evaluate_window(ppg, ppg_times, acc, acc_times, ppg_rate=135, acc_rate=52) -> WindowQuality:
    """Apply fixed experimental gates; values are heuristics, not validated cutoffs."""
    reasons = []
    ppg, acc = np.asarray(ppg, float), np.asarray(acc, float)
    ppg_times, acc_times = np.asarray(ppg_times), np.asarray(acc_times)
    optical = ppg[:, 2] if ppg.ndim == 2 and ppg.shape[1] > 2 else (ppg[:, 0] if ppg.ndim == 2 and ppg.shape[1] else np.array([]))
    if len(optical) < int(ppg_rate * 9.8):
        reasons.append("insufficient_ppg_samples")
    if len(acc) < int(acc_rate * 9.5):
        reasons.append("insufficient_acc_samples")
    ppg_missing = max(0.0, 1 - len(optical) / (ppg_rate * 10))
    acc_missing = max(0.0, 1 - len(acc) / (acc_rate * 10))
    if ppg_missing > .02: reasons.append("ppg_sample_loss_over_2pct")
    if acc_missing > .05: reasons.append("acc_sample_loss_over_5pct")
    finite = np.isfinite(optical)
    if not finite.all(): reasons.append("non_finite_ppg")
    x = optical[finite]
    p2p = float(np.ptp(x)) if len(x) else 0.0
    std = float(np.std(x)) if len(x) else 0.0
    if p2p < 100 or std < 25: reasons.append("low_ppg_amplitude")
    # Avoid assuming a documented sensor ADC full scale: use decoded int32 rails
    # and sustained repeated extreme plateaus as conservative clipping indicators.
    int32 = np.iinfo(np.int32)
    clipped = float(np.mean((x <= int32.min) | (x >= int32.max))) if len(x) else 1.0
    longest_extreme_plateau = 0
    if len(x):
        for extreme in (np.min(x), np.max(x)):
            run = maximum = 0
            for same in x == extreme:
                run = run + 1 if same else 0
                maximum = max(maximum, run)
            longest_extreme_plateau = max(longest_extreme_plateau, maximum)
    if clipped > 0 or longest_extreme_plateau >= max(5, round(ppg_rate * .04)):
        reasons.append("ppg_clipping_or_extreme_plateau")
    bpm, rr_cv, cycles = None, None, 0
    if len(x) >= ppg_rate * 9:
        try:
            signal = sosfiltfilt(butter(3, (.6, 4.0), btype="bandpass", fs=ppg_rate, output="sos"), x)
            candidates = []
            for polarity in (1, -1):
                prominence = max(float(np.std(signal)) * .25, 1e-6)
                peaks, _ = find_peaks(polarity * signal, distance=int(ppg_rate * .35), prominence=prominence)
                rr = np.diff(peaks) / ppg_rate
                rr = rr[(rr >= .35) & (rr <= 1.5)]
                candidate_cv = float(np.std(rr) / max(np.mean(rr), 1e-9)) if len(rr) else float("inf")
                candidates.append((len(rr), candidate_cv, rr))
            cycles, rr_cv, rr = max(candidates, key=lambda item: (item[0], -item[1]))
            if cycles:
                bpm = float(60 / np.median(rr))
        except (ValueError, np.linalg.LinAlgError):
            pass
    if cycles < 5: reasons.append("insufficient_pulse_cycles")
    if bpm is None or not 40 <= bpm <= 170: reasons.append("implausible_or_unidentified_pulse_rate")
    if rr_cv is None or rr_cv > .50: reasons.append("irregular_pulse_periodicity")
    motion_rms, motion_p95, grav = None, None, None
    if len(acc) and np.isfinite(acc).all():
        mag = np.linalg.norm(acc, axis=1)
        grav = float(np.median(mag))
        if not 700 <= grav <= 1300: reasons.append("implausible_accelerometer_gravity_magnitude")
        try:
            dynamic = sosfiltfilt(butter(2, .5, btype="highpass", fs=acc_rate, output="sos"), acc, axis=0)
            dynamic_mag = np.linalg.norm(dynamic, axis=1)
            motion_rms = float(np.sqrt(np.mean(dynamic_mag**2)))
            motion_p95 = float(np.percentile(dynamic_mag, 95))
            if motion_rms > 75 or motion_p95 > 250: reasons.append("excessive_motion")
        except (ValueError, np.linalg.LinAlgError):
            reasons.append("motion_filter_failed")
    residual_ms = None
    if len(ppg_times) and len(acc_times):
        positions = np.searchsorted(acc_times, ppg_times)
        positions = np.clip(positions, 0, len(acc_times)-1)
        residual_ms = float(np.percentile(np.abs(acc_times[positions] - ppg_times), 95) / 1e6)
        if residual_ms > 20: reasons.append("ppg_acc_timestamp_misalignment")
    metrics = {
        "ppg_samples": int(len(optical)), "acc_samples": int(len(acc)),
        "ppg_missing_fraction_estimate": ppg_missing, "acc_missing_fraction_estimate": acc_missing,
        "ppg_peak_to_peak_counts": p2p, "ppg_std_counts": std, "ppg_clipped_fraction": clipped,
        "ppg_longest_extreme_plateau_samples": longest_extreme_plateau,
        "pulse_cycles": cycles, "heart_rate_bpm_estimate": bpm, "pulse_interval_cv": rr_cv,
        "acc_gravity_magnitude_mg": grav, "motion_dynamic_rms_mg": motion_rms,
        "motion_dynamic_p95_mg": motion_p95, "ppg_acc_nearest_sample_p95_ms": residual_ms,
        "configured_ppg_rate_hz": ppg_rate, "configured_acc_rate_hz": acc_rate,
    }
    return WindowQuality(not reasons, metrics, tuple(reasons))


def quality_summary(capture: Capture) -> dict:
    windows = split_synchronized_windows(capture)
    reports = [evaluate_window(*window, capture.sampling_rate_hz, capture.accelerometer_sampling_rate_hz) for window in windows]
    ppg, pt = _samples_with_times(capture)
    acc, at = _samples_with_times(capture, True)
    overlap_s = max(0.0, (min(pt[-1], at[-1]) - max(pt[0], at[0])) / 1e9) if len(pt) and len(at) else 0.0
    return {"overlap_duration_s": overlap_s, "ppg_samples_total": len(ppg), "acc_samples_total": len(acc),
            "ppg_packet_clock": _packet_clock(capture.frames, capture.sampling_rate_hz),
            "acc_packet_clock": _packet_clock(capture.acc_frames, capture.accelerometer_sampling_rate_hz),
            "quality_approved_windows": sum(r.usable for r in reports),
            "windows": [{"usable": r.usable, "metrics": r.metrics, "rejection_reasons": r.rejection_reasons} for r in reports]}
