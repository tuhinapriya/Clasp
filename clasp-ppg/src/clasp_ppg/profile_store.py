"""Local, versioned storage for experimental PaPaGei profiles."""
from __future__ import annotations
import json
import math
import os
import tempfile
from pathlib import Path
from .verifier import EnrollmentProfile

IDENTITY = "primary-user"
MODEL = "papagei-p"


def load_profile(path: str | Path) -> EnrollmentProfile:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if data.get("schemaVersion") not in (1, 2) or data.get("model") != MODEL or data.get("identityId") != IDENTITY:
        raise ValueError("Invalid profile metadata")
    vectors = data.get("embeddings")
    if vectors is None and isinstance(data.get("centroid"), list):
        vectors = [data['centroid']]
    if not isinstance(vectors, list) or not vectors:
        raise ValueError("Insufficient enrollment embeddings")
    for v in vectors:
        if not isinstance(v, list) or len(v) != 512 or any(isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x) for x in v) or sum(x*x for x in v) < 1e-12:
            raise ValueError("Invalid enrollment embedding")
    session = data.get("sourceSessionId")
    if not isinstance(session, str) or not session:
        raise ValueError("Missing enrollment session")
    templates = []
    for key in ('morphologyTemplate', 'apgTemplate'):
        vector = data.get(key)
        if vector is not None and (not isinstance(vector, list) or len(vector) != 64 or any(isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x) for x in vector) or sum(x*x for x in vector) < 1e-12):
            raise ValueError('Invalid morphology template')
        templates.append(vector)
    return EnrollmentProfile(vectors, session, *templates)


def save_profile(path: str | Path, profile: EnrollmentProfile, *, replace: bool = False) -> None:
    path = Path(path)
    if path.exists() and not replace:
        raise FileExistsError("Explicit re-enrollment required")
    payload = {"schemaVersion": 2, "model": MODEL, "identityId": IDENTITY,
               "sourceSessionId": profile.source_session_id, "embeddings": profile.embeddings,
               "centroid": profile.prototype.tolist(), "morphologyTemplate": profile.morphology_template,
               "apgTemplate": profile.apg_template}
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix="ppg-profile-", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            json.dump(payload, out, allow_nan=False, separators=(",", ":"))
            out.flush()
            os.fsync(out.fileno())
        load_profile(temp)
        if replace:
            os.replace(temp, path)
        else:
            os.link(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)
