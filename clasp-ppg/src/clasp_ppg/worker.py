"""Clasp Polar worker, protocol v1: JSON lines on stdout and commands on stdin."""
from __future__ import annotations
import argparse
import asyncio
import json
import sys
import threading
import time
from pathlib import Path
from datetime import datetime, timezone

# Third-party model/SDK messages must not corrupt the protocol.
protocol = sys.stdout
sys.stdout = sys.stderr
lock = threading.Lock()


def emit(kind, **fields):
    with lock:
        protocol.write(json.dumps({'type': kind, 'version': 1, **fields}, allow_nan=False, separators=(',', ':')) + '\n')
        protocol.flush()


async def main(args):
    from .continuous import ContinuousVerifier, PaPaGeiEmbedder
    from .device import PolarPPG
    from .profile_store import load_profile

    sensor = PolarPPG(args.device)
    stop = asyncio.Event()
    runtime = None
    loop = asyncio.get_running_loop()

    def commands():
        for line in sys.stdin:
            try:
                command = json.loads(line)
                if command == {'type': 'shutdown', 'version': 1}:
                    loop.call_soon_threadsafe(stop.set)
                    return
            except (ValueError, TypeError):
                pass
            emit('error', reason='invalid_command')
        loop.call_soon_threadsafe(stop.set)

    threading.Thread(target=commands, daemon=True).start()
    try:
        load_profile(args.profile)
        registered = True
    except (OSError, ValueError, TypeError, KeyError):
        registered = False
    emit('ready', registered=registered, identityId='primary-user' if registered else None)
    emit('profile', registered=registered, identityId='primary-user' if registered else None)

    async def monitor():
        while not stop.is_set():
            s = sensor.status
            now = datetime.now(timezone.utc)
            fresh = lambda t: t is not None and 0 <= (now - t).total_seconds() <= 4
            emit('device', connected=bool(s.connected), streaming=bool(s.streaming and fresh(s.last_sample_at) and fresh(s.last_acc_sample_at)),
                 ppgPackets=s.ppg_packet_count, accPackets=s.acc_packet_count,
                 ppgSamples=s.ppg_sample_count, accSamples=s.acc_sample_count,
                 reason=s.error, observedAt=now.isoformat())
            await asyncio.sleep(1)

    async def capture_without_profile():
        try:
            status = await sensor.connect()
            if not status.connected or not await sensor.wait_for_streaming():
                emit('error', reason=status.error or 'stream_unavailable')
                return
            while not stop.is_set():
                s = sensor.status
                now = datetime.now(timezone.utc)
                if (sensor.disconnect_event.is_set() or not s.connected or not s.streaming or s.error
                    or any(t is None or (now - t).total_seconds() > 4 for t in (s.last_sample_at, s.last_acc_sample_at))):
                    emit('error', reason='stream_failed_or_stale')
                    return
                await asyncio.sleep(.1)
        finally:
            await sensor.disconnect()

    async def enroll():
        from copy import copy
        from .enrollment import enroll_90_seconds, latest_quality
        from .profile_store import save_profile
        if Path(args.profile).exists() and not args.replace:
            emit('enrollment.failed', reason='Existing profile requires explicit re-enrollment.')
            return
        try:
            model = await asyncio.to_thread(PaPaGeiEmbedder, args.papagei_source, args.checkpoint)
            status = await sensor.connect()
            if not status.connected or not await sensor.wait_for_streaming():
                raise RuntimeError(status.error or 'Polar streams unavailable.')
            started = time.monotonic()
            capture_task = asyncio.create_task(sensor.capture('primary-user', 92, session_type='enrollment', abort_event=sensor.disconnect_event))
            quality = 'Waiting for a complete signal and motion window.'
            assessed = 0
            while not capture_task.done():
                await asyncio.sleep(1)
                elapsed = time.monotonic() - started
                now = datetime.now(timezone.utc)
                s = sensor.status
                if stop.is_set() or sensor.disconnect_event.is_set() or not s.connected or not s.streaming or s.error or any(t is None or (now-t).total_seconds() > 4 for t in (s.last_sample_at, s.last_acc_sample_at)):
                    sensor.disconnect_event.set()
                    await capture_task
                    raise RuntimeError('Enrollment stopped: Polar stream disconnected or became stale.')
                complete = min(9, max(0, int((elapsed - 2) // 10)))
                if complete > assessed:
                    with sensor._lock:
                        active = sensor._active_capture
                        snapshot = copy(active) if active else None
                        if snapshot:
                            snapshot.frames = list(active.frames)
                            snapshot.acc_frames = list(active.acc_frames)
                    if snapshot:
                        quality = await asyncio.to_thread(latest_quality, snapshot, complete)
                        assessed = complete
                emit('enrollment.progress', seconds=min(90, round(elapsed)), totalSeconds=90,
                     ppgSamples=s.ppg_sample_count, accSamples=s.acc_sample_count,
                     quality=quality)
            capture = await capture_task
            if time.monotonic() - started < 90 or capture.stream_errors or stop.is_set():
                raise RuntimeError('Enrollment capture incomplete.')
            profile, approved = await asyncio.to_thread(enroll_90_seconds, capture, model)
            now = datetime.now(timezone.utc)
            if stop.is_set() or sensor.disconnect_event.is_set() or not sensor.status.connected or not sensor.status.streaming or sensor.status.error or any(t is None or (now-t).total_seconds() > 4 for t in (sensor.status.last_sample_at, sensor.status.last_acc_sample_at)):
                raise RuntimeError('Enrollment interrupted before saving.')
            save_profile(args.profile, profile, replace=args.replace)
            load_profile(args.profile)
            emit('profile', registered=True, identityId='primary-user')
            emit('enrollment.completed', identityId='primary-user', approvedWindows=approved)
        except Exception as exc:
            emit('enrollment.failed', reason=str(exc) or type(exc).__name__)
        finally:
            await sensor.disconnect()

    monitor_task = asyncio.create_task(monitor())
    task = None
    try:
        if args.enroll:
            task = asyncio.create_task(enroll())
        elif registered:
            try:
                model = await asyncio.to_thread(PaPaGeiEmbedder, args.papagei_source, args.checkpoint)
            except Exception as exc:
                emit('error', reason=f'model_unavailable: {type(exc).__name__}: {exc}')
                return
            runtime = ContinuousVerifier(sensor, args.profile, model, lambda result: emit('verification', **result))
            task = asyncio.create_task(runtime.run())
        else:
            task = asyncio.create_task(capture_without_profile())
        stop_task = asyncio.create_task(stop.wait())
        done, _ = await asyncio.wait({task, stop_task}, return_when=asyncio.FIRST_COMPLETED)
        if stop_task in done and not task.done():
            if runtime:
                runtime.stop()
            else:
                stop.set()
            await task
        else:
            await task
        stop_task.cancel()
    except Exception as exc:
        emit('error', reason=f'worker_failure: {type(exc).__name__}: {exc}')
    finally:
        stop.set()
        monitor_task.cancel()
        try:
            await monitor_task
        except asyncio.CancelledError:
            pass
        emit('device', connected=False, streaming=False, ppgPackets=0, accPackets=0, ppgSamples=0, accSamples=0, reason='worker_stopped', observedAt=datetime.now(timezone.utc).isoformat())
        emit('stopped')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--profile', required=True)
    parser.add_argument('--papagei-source', required=True)
    parser.add_argument('--checkpoint', required=True)
    parser.add_argument('--device')
    parser.add_argument('--enroll', action='store_true')
    parser.add_argument('--replace', action='store_true')
    asyncio.run(main(parser.parse_args()))
