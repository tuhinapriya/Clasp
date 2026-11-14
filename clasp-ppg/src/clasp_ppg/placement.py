"""Guided, explicitly labeled Wearer 1 placement sensitivity experiment."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

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
        if not status.connected or not status.streaming:
            raise RuntimeError(status.error or "Polar PPG+ACC streams did not start")
        print(f"Connected to {status.device_name}. SDK requested raw PPG at {sensor.sampling_rate_hz} Hz and ACC at {sensor.accelerometer_sampling_rate_hz} Hz.")
        captures = {}
        for i, (name, desc) in enumerate(CONDITIONS):
            while True:
                if not sensor.status.connected or not sensor.status.streaming:
                    print("Polar stream is unavailable; reconnecting before another wearer confirmation.")
                    try:
                        await sensor.disconnect()
                    except Exception:
                        pass
                    status = await sensor.connect()
                    if not status.connected or not status.streaming:
                        raise RuntimeError(status.error or "Unable to restart Polar PPG+ACC streams")
                print(f"\nWEARER_1 — {name.upper()}: {desc}.")
                print("Bare upper-arm skin, optical window flat against skin. Remove and reattach the sensor before each condition.")
                await asyncio.to_thread(input, "Confirm WEARER_1 is wearing the sensor in this placement [Enter]: ")
                await asyncio.to_thread(input, "Confirm placement condition and same general site [Enter]: ")
                print(f"Stabilizing and checking live PPG+ACC for {settle_seconds}s; keep the arm still...")
                stable = await sensor.capture("WEARER_1", settle_seconds, session_type=f"{name}_stabilization")
                min_ppg = int(sensor.sampling_rate_hz * settle_seconds * .70)
                min_acc = int(sensor.accelerometer_sampling_rate_hz * settle_seconds * .70)
                if len(stable.frames) == 0 or sum(len(f.samples) for f in stable.frames) < min_ppg or sum(len(f.samples) for f in stable.acc_frames) < min_acc:
                    print(f"Stabilization rejected: PPG packets={len(stable.frames)}, ACC packets={len(stable.acc_frames)}, connection={sensor.status.connected}; reasons={stable.stream_errors}")
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
        profile = enroll_capture(captures["normal"][0], embedder)
        rows = []
        for name, (cap, _folder, report) in captures.items():
            entries = embed_approved_windows(cap, embedder)
            scores = [float(profile.prototype @ e["embedding"]) for e in entries if e["embedding"] is not None]
            rows.append({"condition": name, "similarity_scores": scores, "mean_similarity": sum(scores)/len(scores) if scores else None,
                         "quality_approved_windows": len(scores), "quality": report})
        # Historical held-out impostor is used only as an external score reference.
        impostor_score = None
        historical = Path(__file__).resolve().parents[2] / "data" / "controlled_ppg" / "controlled_20261114T213844Z_9207a4" / "accepted" / "WEARER_2"
        if historical.exists():
            from .storage import capture_matrix
            imp_path = next(historical.glob("*/metadata.json"), None)
            if imp_path:
                imp = load_capture(imp_path.parent)
                matrix = capture_matrix(imp)
                n = imp.sampling_rate_hz * 10
                if len(matrix) >= n:
                    try: impostor_score = float(profile.prototype @ embedder(matrix[:n], imp.sampling_rate_hz))
                    except Exception: impostor_score = None
        if impostor_score is not None:
            for row in rows:
                row["held_out_historical_impostor_similarity"] = impostor_score
                row["genuine_mean_above_impostor"] = row["mean_similarity"] is not None and row["mean_similarity"] > impostor_score
        result = {"threshold": threshold, "threshold_status": "provisional_not_production_calibrated",
                  "enrollment_session": captures["normal"][0].session_id, "comparisons": rows,
                  "all_genuine_conditions_rank_above_impostor": (all(r.get("genuine_mean_above_impostor", False) for r in rows) if impostor_score is not None else None),
                  "historical_impostor_reference": {"source_session": "historical controlled WEARER_2 capture; PPG-only, no motion quality assessment", "similarity": impostor_score},
                  "note": "Historical impostor is a separate prior recording, not threshold-fitting data. Its PPG-only capture has no accelerometer-based quality report. Similarity ranking is descriptive."}
        (run_dir / "placement_results.json").write_text(json.dumps(result, indent=2))
        report_path = run_dir / "PLACEMENT_EXPERIMENT.md"
        lines = ["# Polar PPG placement sensitivity experiment", "", f"Run: `{run_dir.name}`", "", "No model training or threshold fitting was performed.", "", "| Placement | PaPaGei cosine scores | Mean | Clean windows | vs prior impostor |", "|---|---:|---:|---:|---:|"]
        for row in rows:
            comparison = f"{row.get('held_out_historical_impostor_similarity', 0):.4f}" if row.get("genuine_mean_above_impostor") else ("lower" if row.get("held_out_historical_impostor_similarity") is not None else "—")
            lines.append(f"| {row['condition']} | {', '.join(f'{s:.4f}' for s in row['similarity_scores'])} | {row['mean_similarity']:.4f} | {row['quality_approved_windows']}/3 | {comparison} |" if row['mean_similarity'] is not None else f"| {row['condition']} | — | — | 0/3 | — |")
        lines += ["", f"Historical impostor similarity: {impostor_score:.4f}" if impostor_score is not None else "No historical impostor reference was found.", "", "Prior impostor recording is PPG-only and has no accelerometer-based quality assessment. These comparisons are descriptive, not identity-validation statistics.", ""]
        report_path.write_text("\n".join(lines))
        print(f"\nResults saved to {report_path}")
    finally:
        await sensor.disconnect()
