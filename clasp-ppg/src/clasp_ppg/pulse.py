"""Frozen Pulse-PPG inference with the offline benchmark's preprocessing."""
from fractions import Fraction
from pathlib import Path
import sys
import numpy as np
from scipy.signal import cheby2, filtfilt, resample_poly


def live_config(profile_path=None):
    """Load the explicitly experimental, profile-bound local demo fit, if present."""
    import hashlib
    import json
    from .matching import MatchingConfig
    if profile_path is None:
        return MatchingConfig(mode='experimental', threshold=None)
    try:
        profile_path = Path(profile_path)
        fit = json.loads(profile_path.with_name('ppg-demo-thresholds.json').read_text())
        if fit['profile_sha256'] != hashlib.sha256(profile_path.read_bytes()).hexdigest() or fit['purpose'] != 'experimental-demo':
            raise ValueError('Fit does not match current enrollment')
        morphology_max = fit.get('morphology_max')
        return MatchingConfig(mode='experimental', threshold=float(fit['similarity_min']), morphology_threshold=float(morphology_max) if morphology_max is not None else None, morphology_polarity_invariant=fit.get('morphology_polarity_invariant') is True)
    except (OSError, KeyError, ValueError, TypeError):
        return MatchingConfig(mode='experimental', threshold=None)


class PulsePPGEmbedder:
    model_id = 'pulse-ppg'

    def __init__(self, source, checkpoint):
        import torch
        sys.path.insert(0, str(Path(source)))
        from pulseppg.nets.ResNet1D.ResNet1D_Net import Net
        self.torch = torch
        self.model = Net(in_channels=1, base_filters=128, kernel_size=11, stride=2,
                         groups=1, n_block=12, finalpool='max')
        state = torch.load(checkpoint, map_location='cpu', weights_only=True)
        self.model.load_state_dict(state['net'], strict=True)
        self.model.eval()

    @staticmethod
    def preprocess(samples, sampling_rate_hz):
        samples = np.asarray(samples)
        if samples.ndim != 2 or samples.shape[1] < 3 or sampling_rate_hz <= 24:
            raise ValueError('Pulse-PPG requires a valid PPG window and sample rate.')
        channel = samples[:, 2].astype(np.float64)
        if not np.isfinite(channel).all():
            raise ValueError('Non-finite PPG sample.')
        x = (channel - np.mean(channel)) / max(float(np.std(channel)), 1e-12)
        b, a = cheby2(4, 20, [.5, 12], btype='bandpass', fs=sampling_rate_hz)
        x = filtfilt(b, a, x)
        if sampling_rate_hz >= 75:
            n = round(sampling_rate_hz * .05)
            x = filtfilt(np.ones(n) / n, 1, x)
        ratio = Fraction(50, int(sampling_rate_hz))
        x = resample_poly(x, ratio.numerator, ratio.denominator)
        if len(x) < 500 or not np.isfinite(x).all():
            raise ValueError('Pulse-PPG requires a complete 10-second window.')
        return x[:500].astype(np.float32)

    def __call__(self, samples, sampling_rate_hz):
        x = self.preprocess(samples, sampling_rate_hz)
        with self.torch.inference_mode():
            vector = self.model(self.torch.from_numpy(x[None, None, :])).cpu().numpy().reshape(-1)
        norm = float(np.linalg.norm(vector))
        if not np.isfinite(vector).all() or not np.isfinite(norm) or norm < 1e-12:
            raise ValueError('Pulse-PPG returned an invalid embedding.')
        return vector / norm
