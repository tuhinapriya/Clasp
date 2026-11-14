"""Guided, fail-closed collection and scoring for the four-session PPG study."""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import sys
import subprocess
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import numpy as np
from scipy.signal import butter, find_peaks, resample_poly, sosfiltfilt

from .biometric import _distance, _waveforms
from .device import PolarPPG
from .models import Capture
from .quality import assess_quality
from .storage import capture_matrix, save_capture

DURATION_S = 90
SETTLE_S = 25
SESSIONS = (
    ("WEARER_1", "enrollment", "A. Enrollment"),
    ("WEARER_1", "verification_1", "B. First verification after complete sensor removal and reattachment"),
    ("WEARER_2", "impostor", "C. Impostor transfer"),
    ("WEARER_1", "verification_2", "D. Second verification after transfer back"),
)
EXPECTED_PPG_CHANNEL = "ppg_3"


def quality_details(capture: Capture, *, expected_duration_s: float = DURATION_S) -> dict[str, Any]:
    matrix = capture_matrix(capture)
    base = assess_quality(matrix, capture.sampling_rate_hz)
    frames = [frame for frame in capture.frames if frame.samples]
    sample_count = int(len(matrix))
    nominal_fs = float(capture.sampling_rate_hz)
    estimated_missing = 0
    packet_gaps = 0
    packet_jitter_ms: list[float] = []
    for previous, current in zip(frames, frames[1:]):
        delta = (current.sensor_timestamp_ns - previous.sensor_timestamp_ns) / 1e9
        expected_delta = len(current.samples) / nominal_fs
        sample_gap = max(0, int(round(delta * nominal_fs)) - len(current.samples))
        if sample_gap:
            packet_gaps += 1
            estimated_missing += sample_gap
        packet_jitter_ms.append((delta - expected_delta) * 1000.0)

    if len(frames) >= 2:
        first_ns = frames[0].sensor_timestamp_ns - round((len(frames[0].samples) - 1) * 1e9 / nominal_fs)
        last_ns = frames[-1].sensor_timestamp_ns
        sensor_span_s = max((last_ns - first_ns) / 1e9, 0.0)
    else:
        sensor_span_s = sample_count / nominal_fs if nominal_fs else 0.0
    measured_clock_hz = ((sample_count + estimated_missing - 1) / sensor_span_s
                         if sensor_span_s > 0 else 0.0)
    wall_duration_s = ((capture.ended_at - capture.started_at).total_seconds()
                       if capture.ended_at is not None else expected_duration_s)
    measured_delivery_hz = (sample_count / wall_duration_s if wall_duration_s > 0 else 0.0)
    missing_fraction = estimated_missing / max(1, sample_count + estimated_missing)

    if matrix.shape[1] < 3:
        selected_channel = None
    else:
        selected_channel = 2  # fixed optical channel for PaPaGei across all sessions
    signal = matrix[:, selected_channel] if selected_channel is not None and len(matrix) else np.array([])
    finite_signal = signal[np.isfinite(signal)]
    amplitude = {
        "minimum": float(np.min(finite_signal)) if len(finite_signal) else None,
        "maximum": float(np.max(finite_signal)) if len(finite_signal) else None,
        "peak_to_peak": float(np.ptp(finite_signal)) if len(finite_signal) else None,
        "std": float(np.std(finite_signal)) if len(finite_signal) else None,
    }
    # PMD values are decoded signed integers; count integer rail values rather than
    # assuming an undocumented optical ADC full-scale value.
    at_int_rail = int(np.count_nonzero((finite_signal <= np.iinfo(np.int32).min) |
                                       (finite_signal >= np.iinfo(np.int32).max))) if len(finite_signal) else 0
    clipping_fraction = at_int_rail / max(1, len(finite_signal))

    longest_extreme_plateau = 0
    if len(finite_signal):
        for extreme in (np.min(finite_signal), np.max(finite_signal)):
            run = max_run = 0
            for same in finite_signal == extreme:
                run = run + 1 if same else 0
                max_run = max(max_run, run)
            longest_extreme_plateau = max(longest_extreme_plateau, max_run)

    fixed_channel_quality = replace(base, selected_channel=2) if matrix.shape[1] >= 3 else base
    beats = _waveforms(capture, fixed_channel_quality)
    intervals_s: list[float] = []
    pulse_cv = None
    pulse_bpm = None
    valid_cycles = int(len(beats))
    if selected_channel is not None and len(finite_signal) >= int(nominal_fs * 8):
        try:
            sos = butter(3, (0.5, 5.0), btype="bandpass", fs=nominal_fs, output="sos")
            filtered = sosfiltfilt(sos, finite_signal)
            prominence = max(float(np.std(filtered)) * 0.15, 1e-9)
            min_gap = max(1, int(nominal_fs * 0.35))
            peaks_pos, _ = find_peaks(filtered, distance=min_gap, prominence=prominence)
            peaks_neg, _ = find_peaks(-filtered, distance=min_gap, prominence=prominence)
            peaks = peaks_pos if len(peaks_pos) >= len(peaks_neg) else peaks_neg
            intervals = np.diff(peaks) / nominal_fs
            intervals_s = [float(x) for x in intervals if 0.35 <= x <= 1.8]
            if intervals_s:
                pulse_bpm = 60.0 / float(np.median(intervals_s))
                if len(intervals_s) > 1:
                    pulse_cv = float(np.std(intervals_s) / np.mean(intervals_s))
        except ValueError:
            pass

    expected_windows = max(0, int(sample_count // round(nominal_fs * 10)))
    valid_windows = 0
    window_cycle_counts: list[int] = []
    win_n = round(nominal_fs * 10)
    if win_n:
        for start in range(0, sample_count - win_n + 1, win_n):
            window_matrix = matrix[start:start + win_n]
            q = assess_quality(window_matrix, nominal_fs)
            if not q.usable:
                window_cycle_counts.append(0)
                continue
            window_capture = Capture(capture.participant_id, capture.session_id,
                                     capture.session_type, capture.started_at,
                                     sampling_rate_hz=capture.sampling_rate_hz)
            # Reuse the same beat extraction without creating persistent fake data.
            from .models import PPGFrame
            window_capture.frames.append(PPGFrame(0, capture.started_at,
                                                  tuple(tuple(map(int, row)) for row in window_matrix)))
            q = replace(q, selected_channel=2) if window_matrix.shape[1] >= 3 else q
            n_cycles = len(_waveforms(window_capture, q))
            window_cycle_counts.append(n_cycles)
            if n_cycles >= 3:
                valid_windows += 1

    duration_s = sample_count / nominal_fs if nominal_fs else 0.0
    rate_error_pct = abs(measured_clock_hz - nominal_fs) / nominal_fs * 100 if nominal_fs else 100.0
    reasons: list[str] = list(base.reasons)
    if amplitude["std"] is None or amplitude["std"] < 1.0:
        reasons.append("fixed_ppg3_channel_flat_or_missing")
    if duration_s < min(expected_duration_s - 5, 85):
        reasons.append("usable_duration_below_85_seconds")
    if rate_error_pct > 2.0:
        reasons.append("measured_sampling_rate_deviates_over_2_percent")
    if missing_fraction > 0.005:
        reasons.append("estimated_missing_samples_over_0_5_percent")
    if clipping_fraction > 0 or longest_extreme_plateau >= max(5, round(nominal_fs * 0.04)):
        reasons.append("integer_rail_samples_detected")
    if valid_cycles < 25 or len(intervals_s) < 20:
        reasons.append("too_few_identifiable_pulse_cycles")
    if pulse_cv is not None and pulse_cv > 0.50:
        reasons.append("pulse_periodicity_unstable")
    minimum_windows = 2 if expected_duration_s < 40 else 8
    if valid_windows < minimum_windows:
        reasons.append(f"too_few_valid_10_second_windows_{valid_windows}_of_{expected_windows}")
    if capture.stream_errors:
        reasons.append("sensor_stream_errors")
    # Unique reason order, preserving base diagnostic details.
    reasons = list(dict.fromkeys(reasons))
    usable = not reasons
    return {
        "quality": "GOOD" if usable else "BAD",
        "usable": usable,
        "sample_count": sample_count,
        "configured_sampling_rate_hz": nominal_fs,
        "measured_sampling_clock_hz": round(measured_clock_hz, 4),
        "observed_delivery_rate_hz": round(measured_delivery_hz, 4),
        "sampling_rate_error_percent": round(rate_error_pct, 3),
        "estimated_missing_samples": estimated_missing,
        "packet_gaps": packet_gaps,
        "packet_jitter_ms_p95_abs": round(float(np.percentile(np.abs(packet_jitter_ms), 95)), 3) if packet_jitter_ms else None,
        "missing_sample_fraction": round(missing_fraction, 6),
        "usable_duration_seconds": round(duration_s, 3),
        "expected_duration_seconds": expected_duration_s,
        "optical_channel": EXPECTED_PPG_CHANNEL if selected_channel == 2 else None,
        "optical_channel_amplitude_raw_counts": amplitude,
        "samples_at_signed_integer_rails": at_int_rail,
        "longest_repeated_extreme_run_samples": longest_extreme_plateau,
        "valid_pulse_cycles": valid_cycles,
        "median_pulse_rate_bpm": round(pulse_bpm, 2) if pulse_bpm else None,
        "pulse_interval_cv": round(pulse_cv, 4) if pulse_cv is not None else None,
        "valid_10_second_windows": valid_windows,
        "expected_10_second_windows": expected_windows,
        "window_pulse_cycles": window_cycle_counts,
        "accelerometer_motion_assessment": "not available; existing Polar integration records PPG only",
        "stream_errors": capture.stream_errors,
        "reasons": reasons,
    }


def _display_quality(q: dict[str, Any]) -> None:
    amp = q["optical_channel_amplitude_raw_counts"]
    print("\nSignal quality summary")
    print(f"  Quality: {q['quality']}")
    print(f"  PPG: {q['sample_count']:,} samples; configured {q['configured_sampling_rate_hz']:.2f} Hz; "
          f"measured clock {q['measured_sampling_clock_hz']:.2f} Hz; delivery {q['observed_delivery_rate_hz']:.2f} Hz")
    print(f"  Estimated missing: {q['estimated_missing_samples']} samples in {q['packet_gaps']} packet gaps "
          f"({q['missing_sample_fraction']:.3%}); rate deviation {q['sampling_rate_error_percent']:.2f}%")
    print(f"  Usable duration: {q['usable_duration_seconds']:.1f}s; valid 10s windows: "
          f"{q['valid_10_second_windows']}/{q['expected_10_second_windows']}")
    print(f"  {q['optical_channel'] or 'Optical channel'} amplitude (raw counts): "
          f"min={amp['minimum']}, max={amp['maximum']}, peak-to-peak={amp['peak_to_peak']}, SD={amp['std']}")
    print(f"  Pulse cycles: {q['valid_pulse_cycles']}; median pulse rate: {q['median_pulse_rate_bpm']} BPM; "
          f"period CV: {q['pulse_interval_cv']}")
    print(f"  Clipping diagnostics: {q['samples_at_signed_integer_rails']} signed-rail samples; "
          f"longest repeated extreme run {q['longest_repeated_extreme_run_samples']} samples; "
          f"accelerometer: {q['accelerometer_motion_assessment']}")
    if q["reasons"]:
        print("  Rejection reasons: " + ", ".join(q["reasons"]))


async def _prompt_exact(message: str, expected: str) -> bool:
    # Keep the asyncio BLE loop alive while the operator responds in the terminal.
    value = (await asyncio.to_thread(input, message)).strip()
    if value == expected:
        return True
    print(f"Confirmation did not match {expected!r}; no recording started.")
    return False


async def _draw_instructions(wearer: str, session_label: str, attempt: int, run_id: str) -> bool:
    print("\n" + "=" * 74)
    print(f"{session_label} | {wearer} | attempt {attempt} | run {run_id}")
    print(f"The person who should wear the Polar sensor now is: {wearer}")
    print("Position it snugly against bare skin on the upper arm, with the optical window")
    print("flat against the skin. Use the same general upper-arm location/orientation")
    print("for each wearer and each session. Keep the sensor still during recording.")
    print("The software does not identify or infer who is wearing the sensor.")
    if not await _prompt_exact(f"Operator: type {wearer} READY only after verifying the wearer: ", f"{wearer} READY"):
        return False
    if not await _prompt_exact("Operator: type SAME_LOCATION after confirming upper-arm placement: ", "SAME_LOCATION"):
        return False
    return True


async def _countdown_capture(sensor: PolarPPG, wearer: str, seconds: float, session_type: str) -> Capture:
    task = asyncio.create_task(sensor.capture(wearer, seconds, session_type=session_type))
    start = time.monotonic()
    last_shown = 0
    while not task.done():
        await asyncio.sleep(1)
        elapsed = min(int(time.monotonic() - start), int(seconds))
        if elapsed and elapsed % 10 == 0 and elapsed != last_shown:
            active = sensor._active_capture
            if active is not None:
                n = sum(len(frame.samples) for frame in active.frames)
                print(f"  {elapsed}/{int(seconds)} seconds; {n:,} raw samples received")
                last_shown = elapsed
    capture = await task
    if last_shown < int(seconds):
        n = sum(len(frame.samples) for frame in capture.frames)
        print(f"  {int(seconds)}/{int(seconds)} seconds; {n:,} raw samples received")
    return capture


def _persist_capture(capture: Capture, destination: Path, q: dict[str, Any], *,
                     run_id: str, attempt: int, wearer_confirmed: bool,
                     settle_seconds: int) -> Path:
    folder = save_capture(capture, destination)
    metadata_path = folder / "metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["controlled_experiment"] = {
        "run_id": run_id,
        "wearer_label_operator_confirmed": capture.participant_id,
        "attempt": attempt,
        "operator_confirmed_wearer": wearer_confirmed,
        "operator_confirmed_same_general_location": True,
        "placement_instruction": "snug upper arm, optical window against bare skin",
        "settle_seconds": settle_seconds,
        "quality": q,
        "accepted": bool(q["usable"]),
    }
    metadata_path.write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    return folder


async def _wait_for_start() -> bool:
    return await _prompt_exact("Placement is stabilized. Type START to begin the 90-second recording: ", "START")


async def _ensure_stream(sensor: PolarPPG) -> None:
    if sensor.status.connected and sensor.status.streaming:
        return
    print("Polar BLE stream is disconnected; reconnecting before the next sample.")
    status = await sensor.connect()
    if not status.connected or not status.streaming:
        raise RuntimeError(status.error or "Polar PPG stream unavailable after reconnect")
    print(f"Reconnected to {status.device_name} at {sensor.sampling_rate_hz} Hz.")


async def _collect_session(sensor: PolarPPG, root: Path, run_id: str,
                           wearer: str, session_type: str, label: str,
                           settle_seconds: int) -> Path:
    attempt = 0
    while True:
        attempt += 1
        await _ensure_stream(sensor)
        if not await _draw_instructions(wearer, label, attempt, run_id):
            print("No recording made. Reconfirm wearer and placement to continue.")
            continue
        print(f"\nStabilizing placement for {settle_seconds} seconds; this sample is not saved or scored.")
        try:
            await _ensure_stream(sensor)
            warmup = await _countdown_capture(sensor, wearer, settle_seconds, "discarded_stabilization")
        except RuntimeError as exc:
            print(f"Stabilization could not start: {exc}. Reconfirm and retry.")
            await _ensure_stream(sensor)
            continue
        warmup_q = quality_details(warmup, expected_duration_s=settle_seconds)
        # For stabilization, assess raw signal presence/cycles and packet integrity only;
        # the full 90 s duration/window thresholds apply to the accepted session.
        warmup_reasons = []
        if warmup_q["sample_count"] < sensor.sampling_rate_hz * 20:
            warmup_reasons.append("stabilization_sample_too_short")
        if warmup_q["optical_channel_amplitude_raw_counts"]["std"] is None or warmup_q["optical_channel_amplitude_raw_counts"]["std"] < 1:
            warmup_reasons.append("stabilization_flat_signal")
        if warmup_q["valid_pulse_cycles"] < 5:
            warmup_reasons.append("stabilization_pulse_not_identifiable")
        if warmup_q["missing_sample_fraction"] > 0.005:
            warmup_reasons.append("stabilization_packet_loss")
        if warmup_q["samples_at_signed_integer_rails"]:
            warmup_reasons.append("stabilization_clipping_detected")
        if warmup_reasons:
            print("Stabilization signal check was insufficient: " + ", ".join(warmup_reasons))
            if not await _prompt_exact("Adjust the sensor, then type RETRY to repeat wearer/placement confirmation: ", "RETRY"):
                continue
            continue
        print(f"Stabilization check: usable raw signal; {warmup_q['valid_pulse_cycles']} pulse cycles detected.")
        if not await _wait_for_start():
            print("90-second session cancelled; the warm-up was discarded.")
            continue

        print(f"Recording {wearer} for {DURATION_S} seconds. Keep the sensor still.")
        try:
            await _ensure_stream(sensor)
            capture = await _countdown_capture(sensor, wearer, DURATION_S, session_type)
        except RuntimeError as exc:
            print(f"Recording could not start: {exc}. No session was saved; repeat the confirmations.")
            await _ensure_stream(sensor)
            continue
        q = quality_details(capture)
        destination = root / ("accepted" if q["usable"] else "rejected")
        folder = _persist_capture(capture, destination, q, run_id=run_id,
                                  attempt=attempt, wearer_confirmed=True,
                                  settle_seconds=settle_seconds)
        _display_quality(q)
        if q["usable"]:
            print(f"ACCEPTED and saved new session: {folder}")
            return folder
        print(f"REJECTED and retained for audit only: {folder}")
        print("This session will not be analyzed. A fresh recording is required.")
        await asyncio.to_thread(input, "Press Enter when ready to repeat all wearer and placement confirmations: ")


def _score_run(run_dir: Path, papagei_source: Path, checkpoint: Path) -> dict[str, Any]:
    import torch
    from .storage import load_capture
    from .biometric import _distance, _waveforms

    sys.path.insert(0, str(papagei_source))
    from models.resnet import ResNet1D
    model = ResNet1D(in_channels=1, base_filters=32, kernel_size=3, stride=2,
                     groups=1, n_block=18, n_classes=512)
    state = torch.load(checkpoint, map_location="cpu", weights_only=True)
    if isinstance(state, dict) and "state_dict" in state:
        state = state["state_dict"]
    state = {k.removeprefix("module."): v for k, v in state.items()}
    model.load_state_dict(state, strict=True)
    model.eval()

    capture_paths = sorted((run_dir / "accepted").glob("*/*/metadata.json"))
    captures: dict[str, Capture] = {}
    qualities: dict[str, dict[str, Any]] = {}
    embeddings: dict[str, np.ndarray] = {}
    handcrafted: dict[str, np.ndarray] = {}
    for metadata_path in capture_paths:
        capture = load_capture(metadata_path.parent)
        label = capture.session_type
        captures[label] = capture
        meta = json.loads(metadata_path.read_text(encoding="utf-8"))
        qualities[label] = meta["controlled_experiment"]["quality"]
        matrix = capture_matrix(capture)
        channel = capture.channel_names.index(EXPECTED_PPG_CHANNEL)
        samples = matrix[:, channel].astype(np.float64)
        source_fs = float(capture.sampling_rate_hz)
        from scipy.signal import cheby2, filtfilt
        x = (samples - samples.mean()) / max(samples.std(), 1e-12)
        b, a = cheby2(4, 20, [0.5, 12], btype="bandpass", fs=source_fs)
        x = filtfilt(b, a, x)
        if source_fs >= 75:
            smooth_n = max(1, round(source_fs * 0.050))
            x = filtfilt(np.ones(smooth_n) / smooth_n, 1, x)
        # Limit the ratio to a compact rational for practical resampling.
        from fractions import Fraction
        frac = Fraction(125 / source_fs).limit_denominator(1000)
        x = resample_poly(x, frac.numerator, frac.denominator)
        windows = []
        window_cycle_counts = qualities[label]["window_pulse_cycles"]
        for window_index, n_cycles in enumerate(window_cycle_counts):
            start = window_index * 1250
            segment = x[start:start + 1250]
            if (n_cycles >= 3 and len(segment) == 1250 and np.isfinite(segment).all()
                    and float(np.std(segment)) > 1e-8):
                windows.append(segment.astype(np.float32))
        expected = int(qualities[label]["valid_10_second_windows"])
        if len(windows) < expected:
            # Quality windows are counted on 135 Hz raw data; all valid windows
            # should survive the official resampling path. Any drop is explicit.
            print(f"Note: {label}: {len(windows)} model windows from {expected} quality-valid windows.")
        if not windows:
            raise RuntimeError(f"No valid PaPaGei windows for {label}")
        with torch.inference_mode():
            x_tensor = torch.from_numpy(np.stack(windows)[:, None, :])
            vec = model(x_tensor)[0].cpu().numpy()
        vec /= np.maximum(np.linalg.norm(vec, axis=1, keepdims=True), 1e-12)
        embeddings[label] = vec
        q = assess_quality(matrix, source_fs)
        handcrafted[label] = _waveforms(capture, q)

    comparisons = []
    enrollment = "enrollment"
    for label, display in (("verification_1", "WEARER_1 → WEARER_1 (rewear)"),
                           ("impostor", "WEARER_1 → WEARER_2"),
                           ("verification_2", "WEARER_1 → WEARER_1 (second rewear)")):
        scores = embeddings[enrollment] @ embeddings[label].T
        left = handcrafted[enrollment]
        right = handcrafted[label]
        if len(left) < 8 or len(right) < 8:
            baseline_distance = None
        else:
            template_a = np.median(left, axis=0); template_a /= max(np.linalg.norm(template_a), 1e-9)
            template_b = np.median(right, axis=0); template_b /= max(np.linalg.norm(template_b), 1e-9)
            baseline_distance = _distance(template_a, template_b)
        comparisons.append({
            "comparison": display,
            "session": label,
            "papagei_similarity_mean": float(scores.mean()),
            "papagei_similarity_min": float(scores.min()),
            "papagei_similarity_max": float(scores.max()),
            "papagei_window_pairs": int(scores.size),
            "papagei_pairwise_scores": scores.round(8).tolist(),
            "baseline_distance": baseline_distance,
            "enrollment_baseline_cycles": int(len(handcrafted[enrollment])),
            "comparison_baseline_cycles": int(len(right)),
            "quality_enrollment": qualities[enrollment]["quality"],
            "quality_comparison": qualities[label]["quality"],
            "enrollment_usable_windows": qualities[enrollment]["valid_10_second_windows"],
            "comparison_usable_windows": qualities[label]["valid_10_second_windows"],
            "enrollment_paPaGei_windows": int(len(embeddings[enrollment])),
            "comparison_paPaGei_windows": int(len(embeddings[label])),
        })
    genuine = [r["papagei_similarity_mean"] for r in comparisons if r["session"] != "impostor"]
    impostor = [r["papagei_similarity_mean"] for r in comparisons if r["session"] == "impostor"]
    genuine_d = [r["baseline_distance"] for r in comparisons if r["session"] != "impostor" and r["baseline_distance"] is not None]
    impostor_d = [r["baseline_distance"] for r in comparisons if r["session"] == "impostor" and r["baseline_distance"] is not None]
    result = {
        "run_id": run_dir.name,
        "model": "official PaPaGei-P, frozen; output_idx=0; cosine similarity",
        "paPaGei_source": str(papagei_source),
        "checkpoint": str(checkpoint),
        "checkpoint_sha256": hashlib.sha256(checkpoint.read_bytes()).hexdigest(),
        "source_commit": subprocess.run(["git", "-C", str(papagei_source), "rev-parse", "HEAD"],
                                         capture_output=True, text=True, check=False).stdout.strip() or None,
        "sessions": {k: {"wearer_label": captures[k].participant_id,
                         "session_id": captures[k].session_id,
                         "quality": qualities[k],
                         "paPaGei_usable_windows": int(len(embeddings[k])),
                         "handcrafted_pulse_cycles": int(len(handcrafted[k]))}
                     for k in captures},
        "comparisons": comparisons,
        "genuine_rank_above_impostor_papagei": bool(genuine and impostor and min(genuine) > max(impostor)),
        "genuine_rank_below_impostor_baseline": bool(genuine_d and impostor_d and max(genuine_d) < min(impostor_d)),
        "threshold_fit": False,
        "decision": "experimental scores only; no threshold fitted; verifier remains fail-closed",
    }
    return result


def _write_report(result: dict[str, Any], report_path: Path) -> None:
    rows = []
    for r in result["comparisons"]:
        pa = f"{r['papagei_similarity_mean']:.4f} ({r['papagei_similarity_min']:.4f}–{r['papagei_similarity_max']:.4f})"
        base = "n/a" if r["baseline_distance"] is None else f"{r['baseline_distance']:.4f}"
        q = (f"enroll {r['quality_enrollment']}, test {r['quality_comparison']}; "
             f"{r['enrollment_paPaGei_windows']}+{r['comparison_paPaGei_windows']} PaPaGei windows")
        rows.append(f"| {r['comparison']} | {pa} | {base} | {q} |")
    sessions = []
    report_order = [session_type for _, session_type, _ in SESSIONS]
    for name in report_order:
        s = result["sessions"][name]
        q = s["quality"]
        sessions.append(
            f"| {name} | {s['wearer_label']} | {s['session_id']} | {q['quality']} | {q['sample_count']} | "
            f"{q['measured_sampling_clock_hz']:.3f} | {q['estimated_missing_samples']} | "
            f"{q['usable_duration_seconds']:.1f} | {q['optical_channel_amplitude_raw_counts']['peak_to_peak']:.0f} "
            f"({q['optical_channel_amplitude_raw_counts']['std']:.0f}) | "
            f"{q['median_pulse_rate_bpm']} BPM / {q['pulse_interval_cv']} | "
            f"{s['paPaGei_usable_windows']} | {s['handcrafted_pulse_cycles']} |"
        )
    source_commit = result.get("source_commit") or "unknown"
    checkpoint_digest = result.get("checkpoint_sha256") or "unknown"
    text = f"""# Controlled PPG evaluation — {result['run_id']}

## Protocol and provenance

Four new, operator-labeled 90-second Polar Verity Sense recordings were collected in this run. Wearer identity was explicitly confirmed before each recording; it was never inferred by software. Each session used the same instructed upper-arm placement. A 25-second stabilization sample was discarded before each actual recording. Rejected attempts, if any, are retained under `rejected/` and were not scored. No earlier captures were reused.

The raw recordings are in this run's `accepted/` directory. The wearable stream records PPG only; accelerometer motion assessment was unavailable. PaPaGei-P and the handcrafted morphology distance were both evaluated per session. PaPaGei similarity is cosine similarity (larger is closer); baseline distance is normalized pulse-template cosine distance (smaller is closer). No threshold was fitted on these sessions. PaPaGei source commit: `{source_commit}`; checkpoint SHA-256: `{checkpoint_digest}`.

## Session quality

| Session | Operator label | Session ID | Quality | Raw samples | Measured clock Hz | Estimated missing | Usable seconds | PPG p-p (SD) counts | Pulse / interval CV | PaPaGei windows | Baseline cycles |
|---|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
{chr(10).join(sessions)}

Quality acceptance required at least 85 seconds of usable samples, measured sample clock within 2% of configured rate, estimated missing-sample fraction no greater than 0.5%, no signed-integer rail samples, at least 25 identifiable pulse cycles, at least 8 valid 10-second windows, stable pulse interval CV no greater than 0.50 when estimable, and no PPG stream errors. The report includes amplitude range, missing-sample and pulse details in `results.json` and each session's `metadata.json`.

## Comparisons

| Comparison | PaPaGei similarity (mean; min–max) | Baseline distance | Signal quality |
|---|---:|---:|---|
{chr(10).join(rows)}

Genuine comparisons rank above the impostor by PaPaGei: **{result['genuine_rank_above_impostor_papagei']}**. Genuine comparisons rank as closer than the impostor by the handcrafted baseline: **{result['genuine_rank_below_impostor_baseline']}**. These are descriptive scores for one enrolled identity, one impostor, and three test sessions. They do not estimate false-accept or false-reject rates or establish a secure authentication operating point. The existing verifier remains fail-closed.

## Machine-readable detail

See `results.json` and the per-session `metadata.json` files for full metrics, including raw amplitude, packet jitter, pulse cycle counts, and exact pairwise score ranges.
"""
    report_path.write_text(text, encoding="utf-8")


async def run_controlled_experiment(*, data_dir: Path, device: str | None,
                                    papagei_source: Path, checkpoint: Path,
                                    settle_seconds: int = SETTLE_S,
                                    resume_run_id: str | None = None) -> Path:
    if not 20 <= settle_seconds <= 30:
        raise ValueError("settle seconds must be between 20 and 30")
    if not papagei_source.is_dir():
        raise FileNotFoundError(f"Official PaPaGei source directory not found: {papagei_source}")
    if not checkpoint.is_file():
        raise FileNotFoundError(f"PaPaGei checkpoint not found: {checkpoint}")
    if resume_run_id:
        if not re.fullmatch(r"controlled_[A-Za-z0-9_-]{1,64}", resume_run_id):
            raise ValueError("invalid resume run ID")
        run_id = resume_run_id
        run_dir = data_dir / run_id
        if not run_dir.is_dir():
            raise FileNotFoundError(f"run directory does not exist: {run_dir}")
        accepted_paths = sorted((run_dir / "accepted").glob("*/*/metadata.json"))
        accepted_types: list[str] = []
        for path in accepted_paths:
            metadata = json.loads(path.read_text(encoding="utf-8"))
            controlled = metadata.get("controlled_experiment", {})
            if not controlled.get("accepted"):
                raise ValueError(f"resume run contains a non-accepted file under accepted/: {path}")
            accepted_types.append(metadata["session_type"])
        if len(accepted_types) != len(set(accepted_types)):
            raise ValueError("resume run has duplicate accepted session types")
        expected_types = {session_type for _, session_type, _ in SESSIONS}
        if not set(accepted_types).issubset(expected_types):
            raise ValueError("resume run has unexpected accepted session types")
    else:
        run_id = datetime.now(timezone.utc).strftime("controlled_%Y%m%dT%H%M%SZ_") + uuid4().hex[:6]
        run_dir = data_dir / run_id
        run_dir.mkdir(parents=True, exist_ok=False)
        accepted_types = []
    print("Controlled PPG experiment")
    print(f"Run ID: {run_id}")
    print("Four fresh sessions will be collected. No identity will be inferred from the sensor.")
    if accepted_types:
        print(f"Resuming this same run with {len(accepted_types)}/4 accepted sessions; "
              "no recordings from earlier runs will be used.")
    print("Raw captures stay local. Press Ctrl+C to stop; incomplete runs will not be scored.")
    sensor = PolarPPG(device=device, sampling_rate_hz=135)
    try:
        print("Connecting to Polar Verity Sense...")
        status = await sensor.connect()
        if not status.connected or not status.streaming:
            raise RuntimeError(status.error or "Polar PPG stream unavailable")
        print(f"Connected to {status.device_name}; raw PPG stream configured at {sensor.sampling_rate_hz} Hz.")
        accepted: list[Path] = []
        for wearer, session_type, label in SESSIONS:
            if session_type in accepted_types:
                print(f"Skipping {label}: already accepted in this current run.")
                continue
            folder = await _collect_session(sensor, run_dir, run_id, wearer,
                                            session_type, label, settle_seconds)
            accepted.append(folder)
        found_types = [json.loads(p.read_text(encoding="utf-8"))["session_type"]
                       for p in sorted((run_dir / "accepted").glob("*/*/metadata.json"))]
        expected_types = {session_type for _, session_type, _ in SESSIONS}
        if set(found_types) != expected_types or len(found_types) != len(expected_types):
            raise RuntimeError("run does not contain exactly one accepted recording for each required session")
        await sensor.disconnect()
        print("\nAll four new recordings passed signal-quality gates. Scoring frozen PaPaGei-P and the existing handcrafted baseline...")
        result = _score_run(run_dir, papagei_source, checkpoint)
        (run_dir / "results.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        report_path = run_dir / "CONTROLLED_PPG_EVALUATION.md"
        _write_report(result, report_path)
        print("\nComparison results")
        print("Comparison\tPaPaGei similarity\tBaseline distance\tSignal quality / windows")
        for r in result["comparisons"]:
            base = "n/a" if r["baseline_distance"] is None else f"{r['baseline_distance']:.4f}"
            print(f"{r['comparison']}\t{r['papagei_similarity_mean']:.4f} "
                  f"[{r['papagei_similarity_min']:.4f}, {r['papagei_similarity_max']:.4f}]\t{base}\t"
                  f"{r['quality_enrollment']}/{r['quality_comparison']} "
                  f"({r['enrollment_usable_windows']}+{r['comparison_usable_windows']} windows)")
        print(f"Genuine rank above impostor (PaPaGei): {result['genuine_rank_above_impostor_papagei']}")
        print(f"Genuine ranks closer than impostor (baseline): {result['genuine_rank_below_impostor_baseline']}")
        print(f"Threshold fitted: {result['threshold_fit']}; verification remains fail-closed.")
        print(f"\nReport: {report_path}")
        print(f"Machine-readable scores: {run_dir / 'results.json'}")
        return report_path
    finally:
        if sensor.status.connected:
            await sensor.disconnect()
