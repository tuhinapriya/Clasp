import json
import numpy as np
import pytest
from clasp_ppg.matching import ConsecutiveMatcher, MatchingConfig
from clasp_ppg.verifier import EnrollmentProfile
from clasp_ppg.diagnostics import DiagnosticRecorder
from clasp_ppg.profile_store import save_profile, load_profile
from clasp_ppg.continuous import WindowBuffer
from test_motion_verifier import fixture_capture


def profile(n=9): return EnrollmentProfile([[1., 0.] for _ in range(n)], 'fixture')


def test_top_three_aggregation_and_uncalibrated_default():
    p = EnrollmentProfile([[1., 0.], [.8, .6], [0., 1.]] + [[-1., 0.]] * 6, 'fixture')
    matcher = ConsecutiveMatcher(p, MatchingConfig())
    result = matcher.evaluate([1., 0.])
    assert result['similarity'] == pytest.approx(.6)
    assert len(result['templateSimilarities']) == 9
    assert result['decision'] == 'UNCERTAIN'
    assert result['reason'] == 'top_k_threshold_requires_calibration'


def test_consecutive_confirmation_reset_and_ewma():
    matcher = ConsecutiveMatcher(profile(), MatchingConfig(threshold=.8, ewma_alpha=.4))
    for _ in range(4): assert matcher.evaluate([1., 0.])['decision'] == 'UNCERTAIN'
    assert matcher.evaluate([1., 0.])['decision'] == 'MATCH'
    assert matcher.evaluate([0., 1.])['decision'] == 'MISMATCH'
    assert matcher.ewma is None
    assert matcher.evaluate([1., 0.])['decision'] == 'UNCERTAIN'
    matcher.reset()
    assert matcher.streak == 0 and matcher.ewma is None


def test_legacy_cannot_fabricate_nine_templates():
    result = ConsecutiveMatcher(profile(3), MatchingConfig(threshold=.8)).evaluate([1., 0.])
    assert result['decision'] == 'UNCERTAIN'
    assert result['reason'] == 're_enrollment_required_nine_templates'


def test_optional_morphology_and_apg_gates():
    p = profile(); p.morphology_template = [1., 0.]; p.apg_template = [1., 0.]
    matcher = ConsecutiveMatcher(p, MatchingConfig(threshold=.8, morphology_threshold=.1, apg_threshold=.1))
    assert matcher.evaluate([1., 0.])['decision'] == 'UNCERTAIN'
    assert matcher.evaluate([1., 0.], [0., 1.], [1., 0.])['decision'] == 'MISMATCH'
    assert matcher.evaluate([1., 0.], [1., 0.], [0., 1.])['decision'] == 'MISMATCH'


def test_window_buffer_overlaps_by_five_seconds():
    cap = fixture_capture(); buffer = WindowBuffer(step_seconds=5)
    buffer.add(cap.frames[0]); buffer.add(cap.acc_frames[0])
    first, second = buffer.pop(), buffer.pop()
    assert second[1][0] - first[1][0] == pytest.approx(5e9, abs=1e7)
    assert first[1][-1] > second[1][0]


def test_versioned_nine_templates_centroid_and_morphology(tmp_path):
    p = EnrollmentProfile([[1.] + [0.]*511 for _ in range(9)], 'enroll', [1.] + [0.]*63, [0., 1.] + [0.]*62)
    path = tmp_path/'profile.json'; save_profile(path, p)
    raw = json.loads(path.read_text()); loaded = load_profile(path)
    assert raw['schemaVersion'] == 2 and len(raw['embeddings']) == 9 and len(raw['centroid']) == 512
    assert loaded.morphology_template == p.morphology_template and loaded.apg_template == p.apg_template


def test_opt_in_diagnostics_save_raw_and_scores(tmp_path):
    recorder = DiagnosticRecorder(tmp_path, 'person-b', 'primary-user')
    entry = {'embedding': np.array([1., 0.]), 'quality': {'ok': 1}, 'quality_approved': True, 'rejection_reasons': []}
    recorder.record((np.array([[1, 2]]), np.array([4]), np.array([[3, 4, 5]]), np.array([5])), entry,
                    {'decision':'UNCERTAIN','similarity':.9,'templateSimilarities':[.8,.9,.99], 'morphologyDistance':.01, 'apgDistance':.02})
    metadata = json.loads(next(recorder.path.glob('*.json')).read_text())
    assert metadata['testWearer'] == 'person-b' and metadata['enrollmentIdentity'] == 'primary-user'
    assert metadata['decision'] == 'UNCERTAIN' and len(metadata['templateSimilarities']) == 3
    with np.load(next(recorder.path.glob('*.npz'))) as data:
        assert data['ppg_sensor_ns'][0] == 4 and data['acc_sensor_ns'][0] == 5


def test_rejected_window_resets_consecutive_confirmation_and_history():
    from types import SimpleNamespace
    from clasp_ppg.continuous import ContinuousVerifier
    from clasp_ppg.motion_quality import split_synchronized_windows
    sensor = SimpleNamespace(sampling_rate_hz=135, accelerometer_sampling_rate_hz=52)
    matcher = ContinuousVerifier(sensor, 'unused', lambda *_: np.array([1., 0.]), lambda _: None,
                                  config=MatchingConfig(threshold=.8, ewma_alpha=.5))
    matcher.profile = profile()
    clean = split_synchronized_windows(fixture_capture())[0]
    moving = split_synchronized_windows(fixture_capture(moving=True))[0]
    for _ in range(5): matcher.process_window(clean)
    assert matcher.last_result['decision'] == 'MATCH'
    assert matcher.process_window(moving)['decision'] == 'UNCERTAIN'
    assert matcher.matcher.streak == 0 and matcher.matcher.ewma is None
    assert len(matcher.history) == 1
    assert matcher.process_window(clean)['decision'] == 'UNCERTAIN'
    matcher.invalidate('stale_stream')
    assert matcher.last_result['identityId'] is None and matcher.matcher.streak == 0


def test_legacy_centroid_only_profile_loads_without_inventing_templates(tmp_path):
    path = tmp_path/'legacy.json'
    path.write_text(json.dumps({'schemaVersion':1,'model':'papagei-p','identityId':'primary-user',
        'sourceSessionId':'session','centroid':[1.]+[0.]*511}))
    loaded = load_profile(path)
    assert len(loaded.embeddings) == 1
    assert ConsecutiveMatcher(loaded, MatchingConfig(threshold=.8)).evaluate(np.asarray([1.]+[0.]*511))['reason'] == 're_enrollment_required_nine_templates'


def test_enrollment_and_live_reconstruct_packet_overlap_identically():
    from datetime import datetime, timezone
    from clasp_ppg.models import Capture, PPGFrame
    from clasp_ppg.motion_quality import _samples_with_times
    now = datetime.now(timezone.utc)
    frames = [PPGFrame(1_000_000_000, now, ((1,2,3,4), (5,6,7,8))),
              PPGFrame(1_007_000_000, now, ((9,10,11,12), (13,14,15,16)))]
    capture = Capture('primary-user', 'x', 'test', now, sampling_rate_hz=135, frames=frames)
    values, timestamps = _samples_with_times(capture)
    buffer = WindowBuffer()
    for frame in frames: buffer.add(frame)
    assert timestamps.tolist() == [t for t, _ in buffer.data[PPGFrame]]
    assert values.tolist() == [list(v) for _, v in buffer.data[PPGFrame]]


def test_replay_compares_modes_on_recorded_windows(tmp_path):
    from clasp_ppg.replay import replay
    recorder = DiagnosticRecorder(tmp_path, 'person_b', 'primary-user')
    for _ in range(5):
        recorder.record((np.zeros((1350,4)), np.arange(1350), np.zeros((520,3)), np.arange(520)),
                        {'embedding':np.array([1.,0.]), 'quality':{'configured_ppg_rate_hz':135}, 'quality_approved':True, 'rejection_reasons':[]},
                        {'decision':'UNCERTAIN','similarity':1.,'stepSeconds':5})
    rows = replay(recorder.path, profile(), MatchingConfig())
    assert rows[-1]['experimental'] == 'UNCERTAIN'
    assert rows[-1]['experimentalReason'] == 'top_k_threshold_requires_calibration'
    assert rows[-1]['baseline'] == 'MATCH'
    assert all(row['testWearer'] == 'person_b' for row in rows)
