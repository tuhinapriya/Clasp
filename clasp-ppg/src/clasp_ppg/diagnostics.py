"""Explicitly enabled local-only sensitive biometric diagnostics."""
import json
import os
import re
import hashlib
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4
import numpy as np


class DiagnosticRecorder:
    def __init__(self, root, wearer, enrollment_identity):
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', wearer or '') or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', enrollment_identity or ''):
            raise ValueError('Diagnostic recording requires safe wearer and enrollment identity labels')
        self.path = Path(root) / ('ppg-' + uuid4().hex)
        self.path.mkdir(parents=True, mode=0o700, exist_ok=False)
        self.wearer, self.enrollment_identity = wearer, enrollment_identity
        self.count = 0

    def record(self, window, entry, scored):
        self.count += 1
        stem = f'window-{self.count:05d}'
        ppg, ppg_ns, acc, acc_ns = window
        array_path = self.path / (stem + '.npz')
        fd = os.open(array_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, 'wb') as out:
            np.savez_compressed(out, ppg=ppg, ppg_sensor_ns=ppg_ns, accelerometer=acc, acc_sensor_ns=acc_ns,
                                embedding=entry['embedding'] if entry['embedding'] is not None else np.array([]))
        metadata = {'recordedAt': datetime.now(timezone.utc).isoformat(), 'testWearer': self.wearer,
                    'enrollmentIdentity': self.enrollment_identity, 'quality': entry['quality'],
                    'qualityApproved': entry['quality_approved'], 'rejectionReasons': entry['rejection_reasons'], **scored}
        fd = os.open(self.path / (stem + '.json'), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, 'w', encoding='utf8') as out:
            json.dump(metadata, out, allow_nan=False, separators=(',', ':'))


def enrollment_diagnostics_root():
    default_dir = Path(__file__).resolve().parents[3] / '.clasp' / 'ppg-diagnostics'
    return Path(os.environ.get('CLASP_PPG_DIAGNOSTICS_DIR', str(default_dir)))


def save_enrollment_diagnostics(capture, profile, root):
    """Save nine accepted enrollment windows locally; called only after explicit enrollment.

    Sample timestamps use the Polar device clock and are reconstructed within packets.
    The temporary directory is published only after every window has been saved.
    """
    from .motion_quality import split_synchronized_windows, evaluate_window
    windows = split_synchronized_windows(capture, count=9)
    if len(windows) != 9 or len(profile.embeddings) != 9 or profile.source_session_id != capture.session_id:
        raise ValueError('Enrollment diagnostics require nine windows and their matching profile.')
    reports = [evaluate_window(*w, capture.sampling_rate_hz, capture.accelerometer_sampling_rate_hz) for w in windows]
    if any(not r.usable for r in reports):
        raise ValueError('Enrollment diagnostics require nine quality-approved windows.')
    root = Path(root)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    temp = Path(tempfile.mkdtemp(prefix='.enrollment-', dir=root))
    destination = root / ('enrollment-' + uuid4().hex)
    fingerprint = hashlib.sha256(np.asarray(profile.embeddings, dtype='<f4').tobytes()).hexdigest()
    try:
        for index, ((ppg, pt, acc, at), report, embedding) in enumerate(zip(windows, reports, profile.embeddings), 1):
            filename = temp / f'window-{index:05d}.npz'
            fd = os.open(filename, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'wb') as out:
                np.savez_compressed(out, ppg=ppg, ppg_sensor_ns=pt, accelerometer=acc, acc_sensor_ns=at,
                                    embedding=np.asarray(embedding, dtype=np.float32))
            metadata = {'schemaVersion': 1, 'kind': 'enrollment', 'window': index,
                        'testWearer': 'person_a', 'enrollmentIdentity': 'primary-user',
                        'sourceSessionId': capture.session_id, 'embeddingModel': profile.model, 'profileEmbeddingSha256': fingerprint,
                        'samplingRateHz': capture.sampling_rate_hz,
                        'accelerometerSamplingRateHz': capture.accelerometer_sampling_rate_hz,
                        'timestampClock': 'Polar sensor clock; sample times reconstructed within each packet',
                        'qualityApproved': report.usable, 'quality': report.metrics,
                        'rejectionReasons': list(report.rejection_reasons)}
            fd = os.open(temp / f'window-{index:05d}.json', os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'w', encoding='utf8') as out:
                json.dump(metadata, out, allow_nan=False, separators=(',', ':'))
        os.rename(temp, destination)
    except BaseException:
        shutil.rmtree(temp, ignore_errors=True)
        raise
    return destination
