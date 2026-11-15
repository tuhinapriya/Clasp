"""Quality gate for explicit, local 90-second PPG enrollment."""
from .motion_quality import split_synchronized_windows, evaluate_window
from .verifier import enroll_capture


def enroll_90_seconds(capture, embedder):
    if capture.stream_errors:
        raise ValueError('Enrollment capture has stream errors.')
    windows = split_synchronized_windows(capture, count=9)
    reports = [evaluate_window(*w, capture.sampling_rate_hz, capture.accelerometer_sampling_rate_hz) for w in windows]
    if len(reports) != 9 or any(not r.usable for r in reports):
        reasons = sorted({reason for r in reports for reason in r.rejection_reasons})
        raise ValueError('Signal quality insufficient: ' + (', '.join(reasons) or 'incomplete synchronized windows'))
    profile = enroll_capture(capture, embedder, count=9)
    profile.model = getattr(embedder, 'model_id', 'papagei-p')
    if len(profile.embeddings) != 9:
        raise ValueError('Enrollment needs nine usable neural embeddings.')
    from .morphology import pulse_templates
    morphology, apg = pulse_templates(capture)
    if morphology is None or apg is None:
        raise ValueError('Enrollment pulse morphology or APG template is unavailable.')
    profile.morphology_template = morphology.tolist() if morphology is not None else None
    profile.apg_template = apg.tolist() if apg is not None else None
    return profile, len(reports)


def latest_quality(capture, complete_windows):
    windows = split_synchronized_windows(capture, count=complete_windows)
    if not windows:
        return 'Waiting for synchronized PPG and motion samples.'
    report = evaluate_window(*windows[-1], capture.sampling_rate_hz, capture.accelerometer_sampling_rate_hz)
    if report.usable:
        return f"Signal quality passed; motion RMS {report.metrics['motion_dynamic_rms_mg']:.1f} mg."
    return 'Signal or motion needs attention: ' + ', '.join(report.rejection_reasons)
