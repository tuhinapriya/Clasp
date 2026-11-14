from datetime import datetime, timezone

import numpy as np

from clasp_ppg.models import Capture, PPGFrame
from clasp_ppg.quality import assess_quality
from clasp_ppg.storage import capture_matrix, load_capture, save_capture
from clasp_ppg.biometric import EnrollmentStore, calibrate, enroll, verify


def test_quality_rejects_empty_and_flat_data():
    assert assess_quality(np.empty((0, 4)), 135).label == "NO_DATA"
    report = assess_quality(np.zeros((2000, 4)), 135)
    assert not report.usable
    assert "flat_or_missing_optical_signal" in report.reasons


def test_quality_accepts_variable_long_optical_data():
    # Synthetic test fixture only; never used as a live or saved sensor capture.
    t = np.arange(135 * 10) / 135
    pulse = 1200 * np.sin(2 * np.pi * 1.2 * t) + 30 * np.sin(2 * np.pi * 2.4 * t)
    samples = np.column_stack((pulse, pulse * 0.7, pulse * 0.4, np.zeros_like(pulse)))
    report = assess_quality(samples, 135)
    assert report.usable
    assert report.selected_channel == 0


def test_capture_round_trip(tmp_path):
    start = datetime.now(timezone.utc)
    cap = Capture("p1", "s1", "rewear", start, sampling_rate_hz=135)
    cap.frames.append(PPGFrame(2_000_000_000, start, ((10, 20, 30, 40), (11, 21, 31, 41))))
    folder = save_capture(cap, tmp_path)
    loaded = load_capture(folder)
    assert loaded.participant_id == "p1"
    assert loaded.session_type == "rewear"
    assert loaded.frames[0].samples == ((10, 20, 30, 40), (11, 21, 31, 41))


def test_experimental_matcher_fails_closed_without_score_separation(tmp_path):
    # Synthetic waveforms exercise the math only and are not sensor evidence.
    rate = 135
    t = np.arange(rate * 24) / rate
    pulse = 900 * np.sin(2 * np.pi * 1.15 * t) + 250 * np.sin(2 * np.pi * 2.3 * t + 0.4)
    matrix = np.column_stack((pulse, pulse * 0.8, pulse * 0.55, np.zeros_like(pulse))).astype(int)
    now = datetime.now(timezone.utc)
    def make_capture(participant, session, kind):
        capture = Capture(participant, session, kind, now, sampling_rate_hz=rate)
        capture.frames.append(PPGFrame(24_000_000_000, now, tuple(map(tuple, matrix.tolist()))))
        return capture

    capture = make_capture("fixture_user", "fixture_enroll", "baseline")
    genuine = make_capture("fixture_user", "fixture_genuine", "rewear")
    impostor = make_capture("other_fixture", "fixture_impostor", "impostor")
    store = EnrollmentStore(tmp_path / "templates")
    template = enroll("fixture_user", capture, store)
    assert template["beat_count"] >= 20
    result = verify("fixture_user", capture, store)
    assert result.decision == "UNCERTAIN"
    assert result.reason == "calibration_required"
    calibration = calibrate("fixture_user", [genuine], [impostor], store)
    assert calibration["status"] == "NO_SEPARATION"
    result = verify("fixture_user", genuine, store)
    assert result.decision == "UNCERTAIN"
    assert result.reason == "no_genuine_impostor_separation"
