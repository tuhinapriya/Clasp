from datetime import datetime, timezone
import asyncio
from contextlib import nullcontext
from types import SimpleNamespace

import numpy as np

from clasp_ppg.models import ACCFrame, Capture, PPGFrame
from clasp_ppg.motion_quality import quality_summary, split_synchronized_windows
from clasp_ppg.storage import load_capture, save_capture
from clasp_ppg.device import PolarPPG
from clasp_ppg.verifier import EnrollmentProfile, decide_windows, ActiveVerificationSession, _monitor_active_session, PaPaGeiEmbedder


def fixture_capture(moving=False):
    fs, fa, duration = 135, 52, 30
    t = np.arange(fs * duration) / fs
    # Synthetic math fixture, not sensor evidence or a recorded sample.
    pulse = 2000 * np.sin(2 * np.pi * 1.2 * t) + 300 * np.sin(4 * np.pi * 1.2 * t)
    ppg = np.column_stack([pulse, pulse * .8, pulse * .6, np.zeros_like(pulse)]).astype(int)
    at = np.arange(fa * duration) / fa
    acc = np.column_stack([20 * np.sin(2*np.pi*1.8*at), np.zeros_like(at), 1000 + 20*np.cos(2*np.pi*1.8*at)])
    if moving:
        acc[:, 0] += 500 * np.sin(2*np.pi*4*at)
    now = datetime.now(timezone.utc)
    cap = Capture("fixture", "fixture", "test", now, sampling_rate_hz=fs, accelerometer_sampling_rate_hz=fa)
    cap.frames.append(PPGFrame(30_000_000_000, now, tuple(map(tuple, ppg))))
    cap.acc_frames.append(ACCFrame(30_000_000_000, now, tuple(map(tuple, acc.astype(int)))))
    return cap


def test_three_windows_use_shared_polar_clock_and_pass_quiet_fixture():
    cap = fixture_capture()
    windows = split_synchronized_windows(cap)
    assert len(windows) == 3
    report = quality_summary(cap)
    assert report["quality_approved_windows"] == 3
    assert all(w["metrics"]["ppg_acc_nearest_sample_p95_ms"] < 20 for w in report["windows"])


def test_motion_gate_rejects_high_motion_fixture():
    report = quality_summary(fixture_capture(moving=True))
    assert report["quality_approved_windows"] < 3
    assert any("excessive_motion" in w["rejection_reasons"] for w in report["windows"])


def test_verdict_needs_two_approved_windows_and_does_not_mismatch_bad_data():
    profile = EnrollmentProfile([[1., 0.], [1., 0.]], "enroll")
    entries = [
        {"window_index": 1, "quality_approved": True, "quality": {}, "rejection_reasons": [], "embedding": np.array([1., 0.])},
        {"window_index": 2, "quality_approved": False, "quality": {}, "rejection_reasons": ["excessive_motion"], "embedding": None},
        {"window_index": 3, "quality_approved": False, "quality": {}, "rejection_reasons": ["low_ppg_amplitude"], "embedding": None},
    ]
    result = decide_windows(entries, profile)
    assert result["decision"] == "UNCERTAIN"
    assert result["quality_approved_windows"] == 1


def test_verdict_requires_two_of_three_quality_approved_scores():
    profile = EnrollmentProfile([[1., 0.]], "enroll")
    entries = [
        {"window_index": i, "quality_approved": True, "quality": {}, "rejection_reasons": [], "embedding": np.asarray(v)}
        for i, v in enumerate(([1., 0.], [.9, .1], [-1., 0.]), 1)
    ]
    assert decide_windows(entries, profile, .8)["decision"] == "MATCH"
    entries[0]["embedding"] = np.asarray([-1., 0.])
    entries[1]["embedding"] = np.asarray([-1., 0.])
    assert decide_windows(entries, profile, .8)["decision"] == "MISMATCH"


def test_polar_disconnect_immediately_invalidates_active_handle():
    sensor = PolarPPG()
    handle = ActiveVerificationSession()
    sensor._bind_verification(handle)
    sensor._on_log("Link lost: device out of range", "warning")
    assert not handle.active
    assert handle.invalidation_reason == "polar_disconnected"


def test_sdk_intentional_reconnect_log_does_not_abort_stream():
    sensor = PolarPPG()
    sensor.status.connected = True
    sensor.status.streaming = True
    sensor._on_log("Disconnecting...", "info")
    assert sensor.status.connected
    assert sensor.status.streaming
    assert not sensor.disconnect_event.is_set()


def test_active_session_watchdog_invalidates_lost_physiology():
    class FakeStatus:
        connected = True
        streaming = True
        last_sample_at = None
        physiological_signal_present = True
    class FakeSensor:
        status = FakeStatus()
        disconnect_event = asyncio.Event()
        _active_verification = None
    handle = ActiveVerificationSession()
    asyncio.run(_monitor_active_session(FakeSensor(), handle))
    assert not handle.active
    assert handle.invalidation_reason == "physiological_signal_lost"


def test_saved_packet_gap_survives_capture_reload(tmp_path):
    now = datetime.now(timezone.utc)
    cap = Capture("fixture", "gapped", "test", now, sampling_rate_hz=10, channel_names=("ppg_1", "ppg_2", "ppg_3"))
    cap.frames.extend([
        PPGFrame(1_000_000_000, now, ((1, 2, 3), (4, 5, 6))),
        PPGFrame(2_000_000_000, now, ((7, 8, 9),)),
    ])
    folder = save_capture(cap, tmp_path)
    loaded = load_capture(folder)
    assert len(loaded.frames) == 2
    assert loaded.frames[0].sensor_timestamp_ns == 1_000_000_000
    assert loaded.frames[1].sensor_timestamp_ns == 2_000_000_000


def test_combined_stream_status_waits_for_real_callbacks():
    sensor = PolarPPG(stream_ppg=True, stream_accelerometer=True)
    sensor.status.error = "PMD start acknowledged; awaiting data notifications"
    sensor._on_ppg((1_000_000_000, [(0, 0, v, 0) for v in (0, 80, 10, 60)]))
    assert sensor.status.ppg_packet_count == 1
    assert sensor.status.ppg_sample_count == 4
    assert not sensor.status.streaming
    sensor._on_acc((1_000_000_000, [(0, 0, 1000), (0, 0, 1001)]))
    assert sensor.status.acc_packet_count == 1
    assert sensor.status.acc_sample_count == 2
    assert sensor.status.streaming
    assert sensor.status.error is None


def test_papagei_preprocessing_trims_polyphase_ceil_sample_to_1250():
    class Output:
        def __getitem__(self, _index): return self
        def cpu(self): return self
        def numpy(self): return np.array([3.0, 4.0])
    class Model:
        def __call__(self, tensor):
            assert tensor.shape == (1, 1, 1250)
            return Output()
    embedder = object.__new__(PaPaGeiEmbedder)
    embedder.model = Model()
    embedder.torch = SimpleNamespace(from_numpy=lambda value: value, inference_mode=nullcontext)
    t = np.arange(1351) / 135
    samples = np.column_stack([np.sin(t), np.cos(t), np.sin(2*np.pi*1.1*t), np.zeros_like(t)])
    embedding = embedder(samples, 135)
    np.testing.assert_allclose(embedding, [0.6, 0.8])
