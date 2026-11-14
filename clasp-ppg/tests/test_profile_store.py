import pytest
from clasp_ppg.profile_store import load_profile, save_profile
from clasp_ppg.verifier import EnrollmentProfile


def test_profile_roundtrip_requires_explicit_replacement(tmp_path):
    path = tmp_path / 'profile.json'
    profile = EnrollmentProfile([[1.] + [0.] * 511 for _ in range(2)], 'session')
    save_profile(path, profile)
    assert load_profile(path).source_session_id == 'session'
    assert path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        save_profile(path, profile)
    save_profile(path, profile, replace=True)


def test_invalid_profile_not_persisted(tmp_path):
    path = tmp_path / 'profile.json'
    with pytest.raises(ValueError):
        save_profile(path, EnrollmentProfile([[1.], [1.]], 'session'))
    assert not path.exists()
