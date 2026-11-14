from datetime import datetime, timezone
import numpy as np
import pytest
from clasp_ppg.enrollment import enroll_90_seconds
from clasp_ppg.models import Capture, PPGFrame, ACCFrame
from clasp_ppg.profile_store import load_profile, save_profile
from test_motion_verifier import fixture_capture


def long_capture(moving=False):
    fixture = fixture_capture(moving=moving)
    capture = Capture('primary-user', 'fixture-90', 'enrollment', datetime.now(timezone.utc), sampling_rate_hz=135, accelerometer_sampling_rate_hz=52)
    for i in range(3):
        pf, af = fixture.frames[0], fixture.acc_frames[0]
        capture.frames.append(PPGFrame(pf.sensor_timestamp_ns + i*30_000_000_000, pf.received_at, pf.samples))
        capture.acc_frames.append(ACCFrame(af.sensor_timestamp_ns + i*30_000_000_000, af.received_at, af.samples))
    return capture


def test_ninety_second_enrollment_persists_with_explicit_replacement(tmp_path):
    embed = lambda samples, rate: np.array([1.] + [0.]*511)
    profile, count = enroll_90_seconds(long_capture(), embed)
    assert count == 9
    path = tmp_path / 'profile.json'
    save_profile(path, profile)
    assert load_profile(path).source_session_id == 'fixture-90'
    with pytest.raises(FileExistsError): save_profile(path, profile)
    save_profile(path, profile, replace=True)


def test_short_or_moving_capture_is_rejected():
    embed = lambda samples, rate: np.array([1.] + [0.]*511)
    with pytest.raises(ValueError): enroll_90_seconds(fixture_capture(), embed)
    with pytest.raises(ValueError): enroll_90_seconds(long_capture(moving=True), embed)
