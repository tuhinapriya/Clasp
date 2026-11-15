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
    assert len(profile.embeddings) == 9
    path = tmp_path / 'profile.json'
    save_profile(path, profile)
    loaded = load_profile(path)
    assert loaded.source_session_id == 'fixture-90'
    assert len(loaded.embeddings) == 9
    assert len(loaded.morphology_template) == 64 and len(loaded.apg_template) == 64
    with pytest.raises(FileExistsError): save_profile(path, profile)
    save_profile(path, profile, replace=True)


def test_short_or_moving_capture_is_rejected():
    embed = lambda samples, rate: np.array([1.] + [0.]*511)
    with pytest.raises(ValueError): enroll_90_seconds(fixture_capture(), embed)
    with pytest.raises(ValueError): enroll_90_seconds(long_capture(moving=True), embed)


def test_explicit_enrollment_diagnostics_save_all_nine_windows_without_touching_profile(tmp_path):
    import json
    from clasp_ppg.diagnostics import save_enrollment_diagnostics
    capture = long_capture()
    profile, _ = enroll_90_seconds(capture, lambda samples, rate: np.array([1.] + [0.]*511))
    output = save_enrollment_diagnostics(capture, profile, tmp_path / 'diagnostics')
    files = sorted(output.glob('*.npz'))
    assert len(files) == 9 and len(list(output.glob('*.json'))) == 9
    assert output.stat().st_mode & 0o777 == 0o700
    for index, f in enumerate(files, 1):
        meta = json.loads(f.with_suffix('.json').read_text())
        assert meta['kind'] == 'enrollment' and meta['window'] == index
        assert meta['testWearer'] == 'person_a' and meta['enrollmentIdentity'] == 'primary-user'
        assert meta['sourceSessionId'] == profile.source_session_id and meta['qualityApproved']
        assert f.stat().st_mode & 0o777 == 0o600
        with np.load(f, allow_pickle=False) as data:
            assert len(data['ppg']) == len(data['ppg_sensor_ns'])
            assert len(data['accelerometer']) == len(data['acc_sensor_ns'])
            assert data['embedding'].shape == (512,)
    assert not (tmp_path / 'ppg-profile.json').exists()


def test_enrollment_diagnostics_reject_unapproved_windows(tmp_path):
    from clasp_ppg.diagnostics import save_enrollment_diagnostics
    from clasp_ppg.verifier import EnrollmentProfile
    capture = long_capture(moving=True)
    profile = EnrollmentProfile([[1.] + [0.]*511 for _ in range(9)], capture.session_id)
    with pytest.raises(ValueError):
        save_enrollment_diagnostics(capture, profile, tmp_path / 'diagnostics')
    assert not (tmp_path / 'diagnostics').exists()
