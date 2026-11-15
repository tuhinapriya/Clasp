import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import numpy as np

from clasp_ppg.continuous import ContinuousVerifier, WindowBuffer
from clasp_ppg.matching import MatchingConfig
from clasp_ppg.models import PPGFrame, ACCFrame
from clasp_ppg.verifier import EnrollmentProfile
from test_motion_verifier import fixture_capture


def test_non_overlapping_buffers_and_bound():
    cap = fixture_capture()
    buffer = WindowBuffer()
    buffer.add(cap.frames[0]); buffer.add(cap.acc_frames[0])
    windows = []
    while (window := buffer.pop()) is not None:
        windows.append(window)
    assert len(windows) == 2  # The fixture ends just before the final boundary.
    assert windows[0][1][-1] < windows[1][1][0]
    assert len(buffer.data[PPGFrame]) <= 135 * 100


def test_three_windows_refresh_and_fail_closed_on_motion():
    cap = fixture_capture()
    from clasp_ppg.motion_quality import split_synchronized_windows
    window = split_synchronized_windows(cap)[0]
    sensor = SimpleNamespace(sampling_rate_hz=135, accelerometer_sampling_rate_hz=52)
    events = []
    vector = np.array([1., 0.])
    verifier = ContinuousVerifier(sensor, 'unused', lambda *_: vector, events.append, config=MatchingConfig(mode='baseline'))
    verifier.profile = EnrollmentProfile([[1., 0.], [1., 0.]], 'fixture')
    assert [verifier.process_window(window)['decision'] for _ in range(3)] == ['UNCERTAIN', 'UNCERTAIN', 'MATCH']
    vector[:] = [-1., 0.]
    assert verifier.process_window(window)['decision'] == 'MATCH'
    assert verifier.process_window(window)['decision'] == 'MISMATCH'
    moving = split_synchronized_windows(fixture_capture(moving=True))[0]
    assert verifier.process_window(moving)['decision'] == 'UNCERTAIN'
    for _ in range(12): verifier.process_window(window)
    assert len(verifier.history) == 9
    assert events[-1]['identityId'] is None
    assert any(e['decision'] == 'MATCH' and e['identityId'] == 'primary-user' for e in events)


def test_missing_profile_emits_uncertain_and_never_connects(tmp_path):
    class Sensor:
        async def connect(self): raise AssertionError('must not connect')
        async def disconnect(self): pass
    events = []
    asyncio.run(ContinuousVerifier(Sensor(), tmp_path/'missing', None, events.append).run())
    assert events[0]['decision'] == 'UNCERTAIN'
    assert events[0]['reason'] == 'missing_or_invalid_profile'


def test_stale_stream_invalidates_and_disconnects(tmp_path, monkeypatch):
    from clasp_ppg import continuous
    monkeypatch.setattr(continuous, 'load_profile', lambda _: EnrollmentProfile([[1., 0.]] * 9, 'x'))
    class Sensor:
        sampling_rate_hz = 135
        accelerometer_sampling_rate_hz = 52
        disconnect_event = asyncio.Event()
        status = SimpleNamespace(connected=True, streaming=True, error=None,
            last_sample_at=datetime.now(timezone.utc)-timedelta(seconds=20), last_acc_sample_at=datetime.now(timezone.utc))
        async def connect(self): return self.status
        async def wait_for_streaming(self): return True
        def add_frame_listener(self, listener): return lambda: None
        async def disconnect(self): self.closed = True
    sensor = Sensor(); events = []
    asyncio.run(ContinuousVerifier(sensor, tmp_path/'p', None, events.append).run())
    assert any(e['reason'] == 'stale_stream' and e['decision'] == 'UNCERTAIN' for e in events)
    assert sensor.closed


def test_disconnect_invalidates_existing_result():
    sensor = SimpleNamespace(sampling_rate_hz=135, accelerometer_sampling_rate_hz=52)
    events = []
    verifier = ContinuousVerifier(sensor, 'unused', None, events.append)
    verifier.emit('MATCH', similarity=.95)
    verifier.invalidate('polar_disconnected')
    assert verifier.last_result['decision'] == 'UNCERTAIN'
    assert verifier.last_result['identityId'] is None
    assert verifier.last_result['reason'] == 'polar_disconnected'
    assert not verifier.history


def test_missing_samples_clear_previous_match():
    from clasp_ppg.motion_quality import split_synchronized_windows
    sensor = SimpleNamespace(sampling_rate_hz=135, accelerometer_sampling_rate_hz=52)
    verifier = ContinuousVerifier(sensor, 'unused', lambda *_: np.array([1., 0.]), lambda _: None, config=MatchingConfig(mode='baseline'))
    verifier.profile = EnrollmentProfile([[1., 0.], [1., 0.]], 'fixture')
    window = split_synchronized_windows(fixture_capture())[0]
    for _ in range(3): verifier.process_window(window)
    assert verifier.last_result['decision'] == 'MATCH'
    p, pt, a, at = window
    result = verifier.process_window((p[:100], pt[:100], a, at))
    assert result['decision'] == 'UNCERTAIN'
    assert result['identityId'] is None
    assert len(verifier.history) == 1
