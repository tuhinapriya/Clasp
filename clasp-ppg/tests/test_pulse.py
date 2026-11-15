import asyncio
from types import SimpleNamespace
import numpy as np
import pytest
from scipy.signal import cheby2, filtfilt, resample_poly
from clasp_ppg.continuous import ContinuousVerifier
from clasp_ppg.matching import MatchingConfig, ConsecutiveMatcher
from clasp_ppg.profile_store import save_profile, load_profile
from clasp_ppg.pulse import PulsePPGEmbedder, live_config


def test_experimental_fit_is_bound_to_exact_profile(tmp_path):
    import hashlib
    import json
    profile = tmp_path / 'ppg-profile.json'
    profile.write_text('original profile')
    fit = {'purpose': 'experimental-demo', 'profile_sha256': hashlib.sha256(profile.read_bytes()).hexdigest(),
           'similarity_min': 0.81, 'morphology_max': 0.015}
    (tmp_path / 'ppg-demo-thresholds.json').write_text(json.dumps(fit))
    config = live_config(profile)
    assert config.threshold == 0.81 and config.morphology_threshold == 0.015
    profile.write_text('replacement profile')
    assert live_config(profile).threshold is None
from clasp_ppg.verifier import EnrollmentProfile


def test_pulse_preprocessing_matches_offline_benchmark():
    t = np.arange(1350) / 135
    x = 400*np.sin(2*np.pi*1.2*t) + 30*np.cos(2*np.pi*3*t)
    samples = np.column_stack([x*.5, x*.7, x, x*0])
    normalized = (x - x.mean()) / x.std()
    b, a = cheby2(4, 20, [.5,12], btype='bandpass', fs=135)
    y = filtfilt(b,a,normalized)
    n = round(135*.05)
    expected = resample_poly(filtfilt(np.ones(n)/n,1,y),10,27)[:500].astype(np.float32)
    np.testing.assert_array_equal(PulsePPGEmbedder.preprocess(samples,135), expected)


def test_pulse_profile_is_separately_versioned_and_never_silently_replaces(tmp_path):
    path = tmp_path/'profile.json'
    old = EnrollmentProfile([[1.]+[0.]*511 for _ in range(9)], 'old')
    save_profile(path, old)
    pulse = EnrollmentProfile([[1.]+[0.]*511 for _ in range(9)], 'new', [1.]+[0.]*63, [1.]+[0.]*63, model='pulse-ppg')
    with pytest.raises(FileExistsError): save_profile(path, pulse)
    assert load_profile(path).model == 'papagei-p'
    save_profile(path, pulse, replace=True)
    assert load_profile(path).model == 'pulse-ppg'


def test_legacy_profile_is_refused_before_connect(monkeypatch):
    from clasp_ppg import continuous
    monkeypatch.setattr(continuous, 'load_profile', lambda _: EnrollmentProfile([[1.,0.]]*9,'old'))
    class Sensor:
        async def connect(self): raise AssertionError('old profile must never connect')
        async def disconnect(self): pass
    events=[]
    asyncio.run(ContinuousVerifier(Sensor(), 'old', None, events.append, required_model='pulse-ppg').run())
    assert events[0]['decision'] == 'UNCERTAIN'
    assert events[0]['reason'] == 're_enrollment_required_pulse_ppg'


def test_no_calibration_keeps_neural_and_morphology_match_uncertain():
    profile = EnrollmentProfile([[1.,0.]]*9, 'pulse', [1.,0.], [1.,0.], model='pulse-ppg')
    matcher = ConsecutiveMatcher(profile, MatchingConfig())
    for _ in range(8):
        result = matcher.evaluate([1.,0.], [1.,0.], [1.,0.])
        assert result['decision'] == 'UNCERTAIN'
        assert result['similarity'] == 1.0
        assert result['morphologyDistance'] == 0.0


def test_optional_experimental_polarity_tolerant_morphology():
    profile = EnrollmentProfile([[1., 0.]] * 9, 'pulse', [1., 0.], [1., 0.], model='pulse-ppg')
    config = MatchingConfig(threshold=0.9, morphology_threshold=0.1, morphology_polarity_invariant=True, confirmations=1)
    result = ConsecutiveMatcher(profile, config).evaluate([1., 0.], [-1., 0.], [1., 0.])
    assert result['decision'] == 'MATCH'
    assert result['morphologyDistance'] == pytest.approx(0)


def test_live_configuration_does_not_reuse_legacy_thresholds(monkeypatch):
    monkeypatch.setenv('CLASP_PPG_MATCH_MODE', 'baseline')
    monkeypatch.setenv('CLASP_PPG_TOPK_THRESHOLD', '0.8')
    config = live_config()
    assert config.mode == 'experimental' and config.threshold is None
