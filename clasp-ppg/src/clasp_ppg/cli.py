from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from .device import PolarPPG
from .storage import load_capture


def main() -> None:
    parser = argparse.ArgumentParser(prog="clasp-ppg")
    commands = parser.add_subparsers(dest="command", required=True)
    scan = commands.add_parser("scan", help="scan nearby Bluetooth LE Polar sensors")
    scan.add_argument("--timeout", type=float, default=10)
    record = commands.add_parser("record", help="record live raw PPG to local CSV")
    record.add_argument("--participant", required=True)
    record.add_argument("--duration", type=float, default=60)
    record.add_argument("--session-type", default="baseline", help="e.g. baseline or rewear")
    record.add_argument("--device")
    record.add_argument("--data-dir", type=Path, default=Path(__file__).resolve().parents[2] / "data")
    evaluation = commands.add_parser("evaluate", help="report labeled pairwise session scores")
    evaluation.add_argument("--data-dir", type=Path, default=Path(__file__).resolve().parents[2] / "data")
    experiment = commands.add_parser("experiment", help="guided four-session Polar PPG biometric experiment")
    experiment.add_argument("--device", help="Polar BLE name/address; omit to discover nearby sensor")
    experiment.add_argument("--data-dir", type=Path, default=Path(__file__).resolve().parents[2] / "data" / "controlled_ppg")
    experiment.add_argument("--settle-seconds", type=int, default=25, help="placement stabilization duration (20-30 seconds)")
    experiment.add_argument("--papagei-source", type=Path, required=True, help="official PaPaGei repository clone")
    experiment.add_argument("--checkpoint", type=Path, default=Path(__file__).resolve().parents[2] / "weights" / "papagei_p.pt")
    experiment.add_argument("--resume-run-id", help="continue an interrupted current run using only its accepted sessions")
    placement = commands.add_parser("placement-experiment", help="guided normal/tight/loose/repositioned Wearer 1 study")
    placement.add_argument("--device")
    placement.add_argument("--data-dir", type=Path, default=Path(__file__).resolve().parents[2] / "data" / "placement")
    placement.add_argument("--settle-seconds", type=int, default=25)
    placement.add_argument("--papagei-source", type=Path, required=True)
    placement.add_argument("--checkpoint", type=Path, default=Path(__file__).resolve().parents[2] / "weights" / "papagei_p.pt")
    placement.add_argument("--threshold", type=float, default=.80)
    args = parser.parse_args()
    if args.command == "scan":
        asyncio.run(_scan(args.timeout))
    elif args.command == "record":
        asyncio.run(_record(args))
    elif args.command == "evaluate":
        _evaluate(args.data_dir)
    elif args.command == "experiment":
        from .controlled import run_controlled_experiment

        try:
            asyncio.run(run_controlled_experiment(
                data_dir=args.data_dir,
                device=args.device,
                papagei_source=args.papagei_source,
                checkpoint=args.checkpoint,
                settle_seconds=args.settle_seconds,
                resume_run_id=args.resume_run_id,
            ))
        except (FileNotFoundError, RuntimeError, ValueError) as exc:
            raise SystemExit(str(exc)) from exc
    else:
        from .placement import run_placement_experiment
        try:
            asyncio.run(run_placement_experiment(device=args.device, data_dir=args.data_dir,
                papagei_source=args.papagei_source, checkpoint=args.checkpoint,
                settle_seconds=args.settle_seconds, threshold=args.threshold))
        except (FileNotFoundError, RuntimeError, ValueError) as exc:
            raise SystemExit(str(exc)) from exc


async def _scan(timeout: float) -> None:
    from polar_ble_sdk.connector.ble_discovery import discover_polar_devices

    devices = await discover_polar_devices(timeout=timeout)
    if not devices:
        print("No Polar BLE devices found. Confirm sensor mode, battery, Bluetooth, and that Polar Flow is disconnected.")
    for name, address, _device in devices:
        print(f"{name}\t{address}")


async def _record(args) -> None:
    sensor = PolarPPG(device=args.device)
    try:
        status = await sensor.connect()
        if not status.connected or not status.streaming:
            print(json.dumps(status.__dict__, default=str, indent=2))
            raise SystemExit(2)
        print(f"Connected to {status.device_name}; requested {sensor.sampling_rate_hz} Hz raw PPG.")
        capture = await sensor.capture(args.participant, args.duration, session_type=args.session_type, output_dir=str(args.data_dir))
        folder = args.data_dir / args.participant / capture.session_id
        count = sum(len(frame.samples) for frame in capture.frames)
        print(f"Saved {count} real sensor samples to {folder}")
        if not count:
            raise SystemExit("Connected, but received no raw PPG samples; recording is unusable.")
    finally:
        await sensor.disconnect()


def _evaluate(root: Path) -> None:
    from .biometric import evaluate_labeled

    captures = [load_capture(p.parent) for p in root.glob("*/*/metadata.json") if (p.parent / "ppg_samples.csv").exists()]
    report = evaluate_labeled(captures)
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
