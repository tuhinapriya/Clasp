"""Replay opted-in local window diagnostics against both experimental and legacy rules."""
import argparse
import json
from collections import deque
from pathlib import Path
import numpy as np
from .matching import ConsecutiveMatcher, MatchingConfig
from .morphology import window_templates
from .profile_store import load_profile
from .verifier import decide_windows


def replay(folder, profile, config):
    matcher = ConsecutiveMatcher(profile, config)
    baseline = deque(maxlen=3)
    rows = []
    for index, meta_file in enumerate(sorted(Path(folder).glob('window-*.json'))):
        data = json.loads(meta_file.read_text())
        with np.load(meta_file.with_suffix('.npz'), allow_pickle=False) as arrays:
            embedding = arrays['embedding']
            ppg = arrays['ppg']
        quality = bool(data['qualityApproved']) and embedding.size > 0
        entry = {'window_index':index + 1, 'quality_approved':quality, 'quality':data['quality'],
                 'rejection_reasons':data['rejectionReasons'], 'embedding':embedding if quality else None}
        if quality:
            rate = data['quality']['configured_ppg_rate_hz']
            morphology, apg = window_templates(ppg, rate)
            current = matcher.evaluate(embedding, morphology, apg)
        else:
            matcher.reset()
            current = {'decision':'UNCERTAIN', 'reason':'quality_rejected', 'similarity':None}
        # With 5-second-hop diagnostics, every other window reproduces non-overlap baseline inputs.
        if data.get('stepSeconds') != 5 or index % 2 == 0:
            baseline.append(entry)
            prior = 'UNCERTAIN' if len(baseline) < 3 or not all(x['quality_approved'] for x in baseline) else decide_windows(list(baseline), profile)['decision']
        else:
            prior = None
        rows.append({'testWearer':data['testWearer'], 'experimental':current['decision'], 'experimentalScore':current['similarity'],
                     'experimentalReason':current.get('reason'), 'baseline':prior, 'timestamp':data['recordedAt']})
    return rows


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--profile', required=True)
    p.add_argument('folders', nargs='+')
    args = p.parse_args()
    profile = load_profile(args.profile)
    config = MatchingConfig.from_env()
    for folder in args.folders:
        rows = replay(folder, profile, config)
        print(json.dumps({'folder': str(folder), 'rows': rows, 'note':'Window outcomes are descriptive; overlapping windows are not independent trials. Baseline uses alternating windows. No identity label is inferred.'}, allow_nan=False))


if __name__ == '__main__': main()
