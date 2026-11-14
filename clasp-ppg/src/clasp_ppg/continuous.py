"""Experimental continuous Polar verification; decisions are not payment authorization."""
from __future__ import annotations

import asyncio
from collections import deque
from datetime import datetime, timedelta, timezone
from threading import Lock

import numpy as np

from .models import ACCFrame, PPGFrame
from .motion_quality import evaluate_window
from .profile_store import IDENTITY, load_profile
from .verifier import DEFAULT_PROVISIONAL_THRESHOLD, PaPaGeiEmbedder, decide_windows

WINDOW_NS = 10_000_000_000
# Polar ACC notifications may be approximately two seconds apart.
STALE_SECONDS = 4.0


class WindowBuffer:
    """Bounded decoded samples on the common Polar clock, including packet tails."""
    def __init__(self, ppg_rate=135, acc_rate=52):
        self.rates = {PPGFrame: ppg_rate, ACCFrame: acc_rate}
        self.data = {PPGFrame: deque(maxlen=ppg_rate * 100), ACCFrame: deque(maxlen=acc_rate * 100)}
        self.lock = Lock()
        self.next_start = None
        self.last_timestamp = {PPGFrame: None, ACCFrame: None}
        self.invalid = None

    def add(self, frame):
        kind = type(frame)
        if kind not in self.data or not frame.samples:
            return
        rate = self.rates[kind]
        first = frame.sensor_timestamp_ns - round((len(frame.samples) - 1) * 1e9 / rate)
        with self.lock:
            previous = self.last_timestamp[kind]
            if previous is not None and frame.sensor_timestamp_ns <= previous:
                self.invalid = 'non_monotonic_sensor_clock'
                return
            for i, sample in enumerate(frame.samples):
                stamp = first + round(i * 1e9 / rate)
                # Nominal-rate reconstruction may overlap adjacent packets slightly.
                if previous is None or stamp > previous:
                    self.data[kind].append((stamp, sample))
            self.last_timestamp[kind] = frame.sensor_timestamp_ns

    def pop(self):
        with self.lock:
            if self.invalid:
                raise ValueError(self.invalid)
            p, a = self.data[PPGFrame], self.data[ACCFrame]
            if not p or not a:
                return None
            if self.next_start is None:
                self.next_start = max(p[0][0], a[0][0])
            start, end = self.next_start, self.next_start + WINDOW_NS
            # Wait for both streams to pass the boundary; packet batches arrive asynchronously.
            if p[-1][0] < end or a[-1][0] < end:
                return None
            def take(q):
                while q and q[0][0] < start:
                    q.popleft()
                values = []
                while q and q[0][0] < end:
                    values.append(q.popleft())
                return np.asarray([v for _, v in values]), np.asarray([t for t, _ in values], dtype=np.int64)
            pv, pt = take(p)
            av, at = take(a)
            self.next_start = end
            return pv, pt, av, at


class ContinuousVerifier:
    """One BLE connection; emits dictionaries via on_result, including invalidations."""
    def __init__(self, sensor, profile_path, embedder, on_result, *, stale_seconds=STALE_SECONDS):
        self.sensor = sensor
        self.profile_path = profile_path
        self.embedder = embedder
        self.on_result = on_result
        self.stale_seconds = stale_seconds
        self.history = deque(maxlen=9)
        self.buffer = None
        self.profile = None
        self.last_result = None
        self._stop = asyncio.Event()
        self._invalidated = False

    def emit(self, decision, *, similarity=None, quality=None, reason=None):
        now = datetime.now(timezone.utc)
        result = {'decision': decision, 'identityId': IDENTITY if decision == 'MATCH' else None, 'similarity': similarity,
                  'quality': quality, 'verifiedAt': now.isoformat(),
                  'expiresAt': (now + timedelta(seconds=15)).isoformat(),
                  'reason': reason, 'threshold': DEFAULT_PROVISIONAL_THRESHOLD,
                  'experimental': True, 'history': list(self.history)}
        self.last_result = result
        self.on_result(result)
        return result

    def invalidate(self, reason):
        self.history.clear()
        if not self._invalidated:
            self._invalidated = True
            self.emit('UNCERTAIN', reason=reason)

    def process_window(self, window, *, emit=True):
        ppg, pt, acc, at = window
        quality = evaluate_window(ppg, pt, acc, at, self.sensor.sampling_rate_hz,
                                  self.sensor.accelerometer_sampling_rate_hz)
        entry = {'quality_approved': quality.usable, 'quality': quality.metrics,
                 'rejection_reasons': list(quality.rejection_reasons), 'embedding': None}
        if quality.usable:
            try:
                vector = np.asarray(self.embedder(ppg, self.sensor.sampling_rate_hz), dtype=float).reshape(-1)
                if vector.shape != self.profile.prototype.shape or not np.isfinite(vector).all() or np.linalg.norm(vector) < 1e-12:
                    raise ValueError('invalid embedding')
                entry['embedding'] = vector / np.linalg.norm(vector)
            except Exception:
                entry['quality_approved'] = False
                entry['rejection_reasons'].append('embedding_failed')
        # Missing samples immediately invalidate old evidence and restart the 3-window warmup.
        if any('insufficient_' in reason or 'sample_loss' in reason for reason in entry['rejection_reasons']):
            self.history.clear()
            self._invalidated = True
        prior = getattr(self, '_entries', deque(maxlen=3))
        if self._invalidated:
            prior.clear()
        self._invalidated = False
        prior.append(entry)
        self._entries = prior
        scored = decide_windows([entry], self.profile)['windows'][0]
        self.history.append({'similarity': scored['similarity'], 'quality': scored['quality'],
                             'quality_approved': scored['quality_approved'],
                             'rejection_reasons': scored['rejection_reasons'],
                             'timestamp': datetime.now(timezone.utc).isoformat()})
        # The latest window must be usable; no old MATCH survives bad signal.
        if len(prior) < 3 or not all(e['quality_approved'] for e in prior):
            decision = 'UNCERTAIN'
        else:
            decision = decide_windows(list(prior), self.profile)['decision']
        details = {'similarity': scored['similarity'], 'quality': scored['quality'],
                   'reason': ', '.join(scored['rejection_reasons']) or None}
        return self.emit(decision, **details) if emit else (decision, details)

    def stop(self):
        self._stop.set()

    async def run(self):
        unsubscribe = None
        try:
            try:
                self.profile = load_profile(self.profile_path)
            except (OSError, ValueError, KeyError, TypeError):
                self.invalidate('missing_or_invalid_profile')
                return
            self.emit('UNCERTAIN', reason='connecting')
            status = await self.sensor.connect()
            if not status.connected or not await self.sensor.wait_for_streaming():
                self.invalidate(status.error or 'stream_unavailable')
                return
            self.buffer = WindowBuffer(self.sensor.sampling_rate_hz, self.sensor.accelerometer_sampling_rate_hz)
            unsubscribe = self.sensor.add_frame_listener(self.buffer.add)
            self._invalidated = False
            while not self._stop.is_set():
                status = self.sensor.status
                now = datetime.now(timezone.utc)
                if self.sensor.disconnect_event.is_set() or not status.connected or not status.streaming:
                    self.invalidate('polar_disconnected_or_stream_failed')
                    return
                if status.error:
                    self.invalidate('stream_failure')
                    return
                if any(t is None or (now - t).total_seconds() > self.stale_seconds for t in (status.last_sample_at, status.last_acc_sample_at)):
                    self.invalidate('stale_stream')
                    return
                try:
                    window = self.buffer.pop()
                    if window is not None:
                        latest = self.buffer.last_timestamp
                        if (len(window[1]) == 0 or len(window[3]) == 0 or
                            latest[PPGFrame] - int(window[1][-1]) > 4_000_000_000 or
                            latest[ACCFrame] - int(window[3][-1]) > 4_000_000_000):
                            self.invalidate('missing_or_backlogged_samples')
                            return
                        # Run inference off the event loop so disconnect monitoring remains responsive.
                        started = asyncio.get_running_loop().time()
                        task = asyncio.create_task(asyncio.to_thread(self.process_window, window, emit=False))
                        while not task.done():
                            await asyncio.sleep(.1)
                            now = datetime.now(timezone.utc)
                            if (self._stop.is_set() or self.sensor.disconnect_event.is_set() or not status.connected
                                or not status.streaming or status.error or any(t is None or (now - t).total_seconds() > self.stale_seconds for t in (status.last_sample_at, status.last_acc_sample_at))):
                                self.invalidate('stream_failed_or_stale_during_inference')
                                await task
                                return
                        decision, details = await task
                        if asyncio.get_running_loop().time() - started > self.stale_seconds:
                            self.invalidate('verification_processing_stale')
                            return
                        self.emit(decision, **details)
                except Exception:
                    self.invalidate('window_processing_failed')
                    return
                await asyncio.sleep(.1)
        finally:
            if unsubscribe:
                unsubscribe()
            self.invalidate('verification_stopped')
            await self.sensor.disconnect()


def create_continuous_verifier(sensor, profile_path, source, checkpoint, on_result):
    return ContinuousVerifier(sensor, profile_path, PaPaGeiEmbedder(source, checkpoint), on_result)


def main():
    """Run the verifier locally and print JSON results; no enrollment is performed."""
    import argparse
    import json
    from .device import PolarPPG

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profile', required=True, help='Existing primary-user PPG profile JSON')
    parser.add_argument('--papagei-source', required=True)
    parser.add_argument('--checkpoint', required=True)
    parser.add_argument('--device', default=None)
    args = parser.parse_args()
    def output(result):
        print(json.dumps(result, allow_nan=False), flush=True)
    try:
        runtime = create_continuous_verifier(PolarPPG(args.device), args.profile,
                                             args.papagei_source, args.checkpoint, output)
        asyncio.run(runtime.run())
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()
