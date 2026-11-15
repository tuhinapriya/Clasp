"""Configurable experimental scoring; no thresholds are inferred from test data."""
from dataclasses import dataclass
import math
import os
import numpy as np
from .morphology import distance


def optional_float(name):
    value = os.environ.get(name, '').strip()
    return float(value) if value else None


@dataclass(frozen=True)
class MatchingConfig:
    mode: str = 'experimental'
    threshold: float | None = None
    top_k: int = 3
    confirmations: int = 5
    morphology_threshold: float | None = None
    morphology_polarity_invariant: bool = False
    apg_threshold: float | None = None
    ewma_alpha: float | None = None

    def __post_init__(self):
        if self.mode not in ('baseline', 'experimental') or self.top_k < 1 or self.confirmations < 1:
            raise ValueError('Invalid matching configuration')
        if self.threshold is not None and (not math.isfinite(self.threshold) or not -1 <= self.threshold <= 1):
            raise ValueError('Similarity threshold must be finite and within [-1, 1]')
        for x in (self.morphology_threshold, self.apg_threshold):
            if x is not None and (not math.isfinite(x) or not 0 <= x <= 2):
                raise ValueError('Morphology distance threshold must be within [0, 2]')
        if self.ewma_alpha is not None and (not math.isfinite(self.ewma_alpha) or not 0 < self.ewma_alpha <= 1):
            raise ValueError('EWMA alpha must be within (0, 1]')

    @classmethod
    def from_env(cls):
        return cls(mode=os.environ.get('CLASP_PPG_MATCH_MODE', 'experimental'),
                   threshold=optional_float('CLASP_PPG_TOPK_THRESHOLD'),
                   top_k=int(os.environ.get('CLASP_PPG_TOP_K', '3')),
                   confirmations=int(os.environ.get('CLASP_PPG_CONFIRMATIONS', '5')),
                   morphology_threshold=optional_float('CLASP_PPG_MORPHOLOGY_MAX_DISTANCE'),
                   apg_threshold=optional_float('CLASP_PPG_APG_MAX_DISTANCE'),
                   ewma_alpha=optional_float('CLASP_PPG_EWMA_ALPHA'))


class ConsecutiveMatcher:
    def __init__(self, profile, config):
        self.profile, self.config = profile, config
        self.reset()

    def reset(self):
        self.streak = 0
        self.ewma = None

    def evaluate(self, embedding, morphology=None, apg=None):
        c = self.config
        if len(self.profile.embeddings) < 9:
            self.reset()
            return {'decision': 'UNCERTAIN', 'reason': 're_enrollment_required_nine_templates', 'similarity': None, 'templateSimilarities': []}
        templates = np.asarray(self.profile.embeddings, dtype=float)
        norms = np.linalg.norm(templates, axis=1)
        vector = np.asarray(embedding, dtype=float)
        norm = np.linalg.norm(vector)
        if not np.isfinite(templates).all() or not np.isfinite(norm) or norm < 1e-9 or np.any(norms < 1e-9) or vector.shape != templates.shape[1:]:
            raise ValueError('Invalid PaPaGei embedding')
        similarities = np.clip((templates / norms[:, None]) @ (vector / norm), -1, 1)
        if c.top_k > len(similarities):
            raise ValueError('top_k exceeds enrollment templates')
        similarity = float(np.mean(np.sort(similarities)[-c.top_k:]))
        md = distance(morphology, self.profile.morphology_template)
        if md is not None and c.morphology_polarity_invariant:
            md = min(md, 2.0 - md)
        ad = distance(apg, self.profile.apg_template)
        output = {'similarity': similarity, 'templateSimilarities': similarities.tolist(), 'morphologyDistance': md, 'apgDistance': ad}
        if c.threshold is None:
            self.reset()
            return {**output, 'decision': 'UNCERTAIN', 'reason': 'top_k_threshold_requires_calibration'}
        for name, value, maximum in (('morphology', md, c.morphology_threshold), ('apg', ad, c.apg_threshold)):
            if maximum is not None and value is None:
                self.reset()
                return {**output, 'decision': 'UNCERTAIN', 'reason': f'{name}_template_or_signal_unavailable'}
            if maximum is not None and value > maximum:
                self.reset()
                return {**output, 'decision': 'MISMATCH', 'reason': f'{name}_distance_exceeded'}
        # Raw failures always reset before smoothing; EWMA never carries a prior MATCH.
        if similarity < c.threshold:
            self.reset()
            return {**output, 'decision': 'MISMATCH', 'reason': 'similarity_below_threshold'}
        self.ewma = similarity if self.ewma is None or c.ewma_alpha is None else c.ewma_alpha * similarity + (1-c.ewma_alpha) * self.ewma
        self.streak += 1
        return {**output, 'smoothedSimilarity': self.ewma, 'consecutive': self.streak,
                'decision': 'MATCH' if self.streak >= c.confirmations and self.ewma >= c.threshold else 'UNCERTAIN',
                'reason': None if self.streak >= c.confirmations and self.ewma >= c.threshold else 'collecting_consecutive_confirmations'}
