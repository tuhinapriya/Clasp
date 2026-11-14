"""Guided, explicitly labeled Wearer 1 placement sensitivity experiment."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from .device import PolarPPG
from .motion_quality import quality_summary
from .storage import load_capture, save_capture
from .verifier import PaPaGeiEmbedder, enroll_capture, embed_approved_windows


CONDITIONS = (
    ("normal", "snug, comfortable placement at the agreed upper-arm site"),
    ("tighter", "same site, slightly tighter than normal but not uncomfortable"),
    ("looser", "same site, slightly looser while maintaining optical contact"),
    ("repositioned", "a nearby upper-arm position with the same sensor orientation"),
)


async def run_placement_experiment(*, device, data_dir: Path, papagei_source: Path, checkpoint: Path,
                                   settle_seconds=25, threshold=.80):
    embedder = PaPaGeiEmbedder(papagei_source, checkpoint)
    sensor = PolarPPG(device=device)
    run_dir = data_dir / f"placement_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    accepted = run_dir / "accepted"
    accepted.mkdir(parents=True, exist_ok=True)
    try:
        status = await sensor.connect()
        if not status.connected or not status.ppg_stream_started or not status.acc_stream_started:
            raise RuntimeError(status.error or "Polar PPG+ACC PMD streams did not start")
        print(f"Connected to {status.device_name}. SDK requested raw PPG at {sensor.sampling_rate_hz} Hz and ACC at {sensor.accelerometer_sampling_rate_hz} Hz.")
        captures = {}
        for i, (name, desc) in enumerate(CONDITIONS):
            while True:
                if (not sensor.status.connected or not sensor.status.ppg_stream_started
                        or not sensor.status.acc_stream_started):
                    print("Polar stream is unavailable; reconnecting before another wearer confirmation.")
                    try:
                        await sensor.disconnect()
                    except Exception:
                        pass
                    status = await sensor.connect()
                    if not status.connected or not status.ppg_stream_started or not status.acc_stream_started:
                        raise RuntimeError(status.error or "Unable to restart Polar PPG+ACC streams")
                print(f"\nWEARER_1 — {name.upper()}: {desc}.")
                print("Bare upper-arm skin, optical window flat against skin. Remove and reattach the sensor before each condition.")
                await asyncio.to_thread(input, "Confirm WEARER_1 is wearing the sensor in this placement [Enter]: ")
                await asyncio.to_thread(input, "Confirm placement condition and same general site [Enter]: ")
                print(f"Stabilizing and checking live PPG+ACC for {settle_seconds}s; keep the arm still...")
                stable = await sensor.diagnostic_listen(settle_seconds, query_supported_settings=False)
                deltas = stable["callback_deltas"]
                if deltas["ppg_packets"] == 0 or deltas["acc_packets"] == 0:
                    print(f"Stabilization rejected: PPG packets={deltas['ppg_packets']}, ACC packets={deltas['acc_packets']}, connection={stable['ble_client_connected']}; errors={stable['stream_errors']}")
                    print("Reconfirm wearer and placement before another stabilization attempt.")
                    continue
                cap = await sensor.capture("WEARER_1", 32, session_type=name)
                report = quality_summary(cap)
                folder = save_capture(cap, accepted)
                (folder / "placement.json").write_text(json.dumps({"condition": name, "operator_confirmed_wearer": "WEARER_1", "quality": report}, indent=2))
                print(f"Quality: {report['quality_approved_windows']}/3 windows accepted; session {cap.session_id}")
                if cap.stream_errors:
                    print(f"Stream warnings: {cap.stream_errors}")
                for n, w in enumerate(report["windows"], 1):
                    print(f"  window {n}: {'GOOD' if w['usable'] else 'REJECTED'} {w['rejection_reasons']} {w['metrics']}")
                if report["quality_approved_windows"] >= 2:
                    captures[name] = (cap, folder, report)
                    break
                print("Recording rejected; retake this condition after repeating both confirmations.")
            # separate sensor removal/reattachment required for every session
        result, report_path = score_saved_placement_run(run_dir, embedder, threshold=threshold)
        print(f"\nResults saved to {report_path}")
    finally:
        await sensor.disconnect()


def score_saved_placement_run(run_dir: Path, embedder, *, threshold=.80):
    """Score already captured separate placement sessions; useful after interruption."""
    accepted = run_dir / "accepted"
    captures = {}
    reports = {}
    for metadata_path in accepted.glob("*/*/metadata.json"):
        cap = load_capture(metadata_path.parent)
        placement = json.loads((metadata_path.parent / "placement.json").read_text())
        condition = placement["condition"]
        captures[condition] = cap
        reports[condition] = placement["quality"]
    missing = {name for name, _ in CONDITIONS} - captures.keys()
    if missing:
        raise RuntimeError(f"Cannot score incomplete placement run; missing {sorted(missing)}")
    profile = enroll_capture(captures["normal"], embedder)
    rows = []
    for name, _description in CONDITIONS:
        entries = embed_approved_windows(captures[name], embedder)
        scores = [float(profile.prototype @ entry["embedding"])
                  for entry in entries if entry["embedding"] is not None]
        rows.append({"condition": name, "session_id": captures[name].session_id,
            "similarity_scores": scores, "mean_similarity": sum(scores)/len(scores) if scores else None,
            "quality_approved_windows": len(scores),
            "windows_at_provisional_threshold": sum(score >= threshold for score in scores),
            "quality": reports[name]})
    impostor_scores = []
    historical = Path(__file__).resolve().parents[2] / "data" / "controlled_ppg" / "controlled_20261114T213844Z_9207a4" / "accepted" / "WEARER_2"
    if historical.exists():
        from .storage import capture_matrix
        imp_path = next(historical.glob("*/metadata.json"), None)
        if imp_path:
            imp = load_capture(imp_path.parent)
            matrix = capture_matrix(imp)
            n = imp.sampling_rate_hz * 10
            for start in range(0, len(matrix) - n + 1, n):
                try:
                    impostor_scores.append(float(profile.prototype @ embedder(matrix[start:start+n], imp.sampling_rate_hz)))
                except Exception:
                    continue
    impostor_score = float(np.mean(impostor_scores)) if impostor_scores else None
    impostor_max = max(impostor_scores) if impostor_scores else None
    if impostor_score is not None:
        for row in rows:
            row["held_out_historical_impostor_similarity"] = impostor_score
            row["genuine_mean_above_impostor"] = row["mean_similarity"] is not None and row["mean_similarity"] > impostor_score
            row["mean_minus_impostor"] = row["mean_similarity"] - impostor_score if row["mean_similarity"] is not None else None
            row["genuine_windows_above_impostor_max"] = sum(score > impostor_max for score in row["similarity_scores"])
    result = {"threshold": threshold, "threshold_status": "provisional_not_production_calibrated",
        "enrollment_session": captures["normal"].session_id, "comparisons": rows,
        "all_genuine_conditions_rank_above_impostor": (all(r.get("genuine_mean_above_impostor", False) for r in rows) if impostor_score is not None else None),
        "all_genuine_windows_above_impostor_max": (all(r.get("genuine_windows_above_impostor_max", 0) == len(r["similarity_scores"]) for r in rows) if impostor_max is not None else None),
        "historical_impostor_reference": {"source_session": "historical controlled WEARER_2 capture; PPG-only, no motion quality assessment", "window_scores": impostor_scores, "mean_similarity": impostor_score, "max_similarity": impostor_max},
        "note": "Historical impostor is a separate prior recording, not threshold-fitting data. The mean aggregates its non-overlapping 10-second windows; those adjacent windows are not independent trials. Its PPG-only capture has no accelerometer-based quality assessment. Normal is the enrollment session itself; its score is not an independent genuine test. Placement scores are descriptive."}
    (run_dir / "placement_results.json").write_text(json.dumps(result, indent=2))
    report_path = run_dir / "PLACEMENT_EXPERIMENT.md"
    lines = ["# Polar PPG placement sensitivity experiment", "", f"Run: `{run_dir.name}`", "", "No model training or threshold fitting was performed. Provisional threshold: 0.80.", "", f"Historical impostor-window mean: {impostor_score:.4f}; max: {impostor_max:.4f}.", "", "| Placement | Session | PaPaGei cosine scores | Mean | Clean windows | Windows ≥0.80 | Genuine windows > impostor max |", "|---|---|---:|---:|---:|---:|---:|"]
    for row in rows:
        above_max = row.get("genuine_windows_above_impostor_max", 0)
        scores = ", ".join(f"{score:.4f}" for score in row["similarity_scores"])
        lines.append(f"| {row['condition']} | `{row['session_id']}` | {scores or '—'} | {row['mean_similarity']:.4f} | {row['quality_approved_windows']}/3 | {row['windows_at_provisional_threshold']}/{row['quality_approved_windows']} | {above_max}/{len(row['similarity_scores'])} |" if row['mean_similarity'] is not None else f"| {row['condition']} | `{row['session_id']}` | — | — | 0/3 | 0/0 | 0/0 |")
    lines += ["", f"Historical impostor window similarities: {', '.join(f'{score:.4f}' for score in impostor_scores)}" if impostor_score is not None else "No historical impostor reference was found.", "", "Prior impostor recording is PPG-only and has no accelerometer-based quality assessment. Its adjacent windows are not independent trials. The normal row is the same session used for enrollment; its similarity is not an independent test. Comparisons are descriptive.", ""]
    report_path.write_text("\n".join(lines))
    return result, report_path
