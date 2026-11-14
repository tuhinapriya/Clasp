"""Local ECAPA-TDNN enrollment, speaker matching, and speech-to-text for Vexo PCM."""
from __future__ import annotations

import json
import os
import tempfile
import time
import uuid
from pathlib import Path

MODEL_ID = "speechbrain/spkrec-ecapa-voxceleb"
IDENTITY_ID = "primary-user"
ASR_MODEL_ID = os.environ.get("CLASP_ASR_MODEL", "mlx-community/whisper-tiny")
SAMPLE_RATE = 16_000
FRAME_SAMPLES = 320
MIN_CAPTURE_SECONDS = 4.0
MIN_SPEECH_SECONDS = 2.5
MIN_SPEECH_RATIO = 0.30
MATCH_THRESHOLD = float(os.environ.get("CLASP_VOICE_MATCH_THRESHOLD", "0.28"))
MISMATCH_THRESHOLD = float(os.environ.get("CLASP_VOICE_MISMATCH_THRESHOLD", "0.22"))
EVIDENCE_TTL_SECONDS = 15


class VoiceQualityError(Exception):
    pass


def speech_quality(pcm: bytes) -> dict:
    import numpy as np

    samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
    frame_count = len(samples) // FRAME_SAMPLES
    if frame_count == 0:
        raise VoiceQualityError("No decoded Vexo microphone audio was received.")
    frames = samples[:frame_count * FRAME_SAMPLES].reshape(frame_count, FRAME_SAMPLES)
    rms = np.sqrt(np.mean(np.square(frames), axis=1))
    speech = rms >= 0.012
    active_seconds = float(np.count_nonzero(speech) * FRAME_SAMPLES / SAMPLE_RATE)
    speech_ratio = float(np.mean(speech))
    clipping_ratio = float(np.mean(np.abs(samples) >= 0.995))
    seconds = len(samples) / SAMPLE_RATE
    if seconds < MIN_CAPTURE_SECONDS:
        raise VoiceQualityError(f"Capture was too short ({seconds:.1f}s; need at least {MIN_CAPTURE_SECONDS:.0f}s).")
    if active_seconds < MIN_SPEECH_SECONDS or speech_ratio < MIN_SPEECH_RATIO:
        raise VoiceQualityError("Not enough clear speech was detected. Speak naturally for several seconds and retry.")
    if clipping_ratio > 0.05:
        raise VoiceQualityError("The recording was clipped or distorted; reposition the band and retry.")
    framed_samples = samples[:frame_count * FRAME_SAMPLES]
    speech_samples = framed_samples[np.repeat(speech, FRAME_SAMPLES)]
    return {
        "samples": speech_samples.astype(np.float32),
        "capturedSeconds": round(seconds, 2),
        "speechSeconds": round(active_seconds, 2),
        "speechRatio": round(speech_ratio, 3),
        "clippingRatio": round(clipping_ratio, 4),
    }


class LocalSpeakerVerifier:
    def __init__(self, profile_path: str | Path, model_dir: str | Path, embedder=None, transcriber=None, now=time.time):
        self.profile_path = Path(profile_path)
        self.model_dir = Path(model_dir)
        self._embedder = embedder
        self._transcriber = transcriber
        self._model = None
        self._torch = None
        import numpy as np
        self._np = np
        self._now = now

    def load(self, on_status=None):
        if self._embedder is None and self._model is None:
            if on_status:
                on_status("loading-model", MODEL_ID)
            import torch
            from speechbrain.inference.speaker import SpeakerRecognition

            self._torch = torch
            self._model = SpeakerRecognition.from_hparams(
                source=MODEL_ID,
                savedir=str(self.model_dir),
                run_opts={"device": "cpu"},
            )
            self._model.eval()
        return MODEL_ID

    def _embedding(self, samples):
        if self._embedder is not None:
            vector = self._embedder(samples)
        else:
            np = self._np
            torch = self._torch
            waveform = torch.from_numpy(np.asarray(samples, dtype=np.float32)).unsqueeze(0)
            with torch.inference_mode():
                vector = self._model.encode_batch(waveform).detach().cpu().numpy().reshape(-1)
        vector = self._np.asarray(vector, dtype=self._np.float32).reshape(-1)
        norm = float(self._np.linalg.norm(vector))
        if not self._np.isfinite(norm) or norm <= 1e-8 or not self._np.all(self._np.isfinite(vector)):
            raise VoiceQualityError("The speaker model could not produce a usable embedding.")
        return (vector / norm).tolist()

    def _profile(self):
        try:
            profile = json.loads(self.profile_path.read_text(encoding="utf-8"))
            embedding = profile["embedding"]
            if profile.get("model") != MODEL_ID or len(embedding) != 192 or not profile.get("identityId"):
                return None
            if any(not isinstance(v, (int, float)) for v in embedding):
                return None
            return profile
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def profile_metadata(self):
        profile = self._profile()
        if not profile:
            return {"registered": self.profile_path.exists(), "identityId": None, "enrolledAt": None, "model": MODEL_ID, "reEnrollmentRequired": self.profile_path.exists()}
        return {
            "registered": True,
            "identityId": profile["identityId"],
            "enrolledAt": profile["enrolledAt"],
            "model": profile["model"],
            "reEnrolledAt": profile.get("reEnrolledAt"),
            "profileId": profile.get("profileId"),
            "reEnrollmentRequired": profile["identityId"] != IDENTITY_ID,
        }

    def register(self, pcm: bytes, replace=False):
        existing = self._profile()
        if self.profile_path.exists() and not replace:
            raise FileExistsError("A voice profile already exists. Confirm explicit re-enrollment first.")
        quality = speech_quality(pcm)
        embedding = self._embedding(quality["samples"])
        now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self._now()))
        profile = {
            "schemaVersion": 1,
            "identityId": IDENTITY_ID,
            "profileId": str(uuid.uuid4()),
            "model": MODEL_ID,
            "embedding": embedding,
            "enrolledAt": existing.get("enrolledAt", now_iso) if existing else now_iso,
            **({"reEnrolledAt": now_iso} if existing else {}),
        }
        self.profile_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix="voice-profile-", suffix=".tmp", dir=self.profile_path.parent)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(profile, stream, separators=(",", ":"))
                stream.flush()
                os.fsync(stream.fileno())
            if replace:
                os.replace(temporary, self.profile_path)
            else:
                os.link(temporary, self.profile_path)
            os.chmod(self.profile_path, 0o600)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return {**self.profile_metadata(), **quality_summary(quality)}

    def validate(self, pcm: bytes, source: dict):
        now = self._now()
        timestamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))
        expires = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now + EVIDENCE_TTL_SECONDS))
        base = {
            "factor": "voice-speaker",
            "identityId": None,
            "verifier": MODEL_ID,
            "verifiedAt": timestamp,
            "expiresAt": expires,
            "simulated": False,
            "audioSource": source,
            "similarity": None,
        }
        try:
            quality = speech_quality(pcm)
            profile = self._profile()
            if profile is None:
                return {**base, **quality_summary(quality), "audioUsable": True, "decision": "UNCERTAIN", "reason": "No registered voice profile exists."}
            if profile["identityId"] != IDENTITY_ID:
                return {**base, **quality_summary(quality), "audioUsable": True, "decision": "UNCERTAIN", "reason": "Legacy voice profile requires explicit re-enrollment for primary-user."}
            embedding = self._embedding(quality["samples"])
            np = self._np
            score = float(np.dot(np.asarray(embedding), np.asarray(profile["embedding"])))
            if not np.isfinite(score):
                raise VoiceQualityError("Speaker similarity was not finite.")
            if score >= MATCH_THRESHOLD:
                decision = "MATCH"
                base["identityId"] = IDENTITY_ID
            elif score <= MISMATCH_THRESHOLD:
                decision = "MISMATCH"
            else:
                decision = "UNCERTAIN"
            return {**base, **quality_summary(quality), "audioUsable": True, "decision": decision, "similarity": round(score, 5), "reason": f"ECAPA cosine similarity {score:.3f}; match threshold {MATCH_THRESHOLD:.2f}."}
        except VoiceQualityError as error:
            return {**base, "audioUsable": False, "decision": "UNCERTAIN", "reason": str(error)}

    def transcribe(self, pcm: bytes):
        if self._transcriber is not None:
            return str(self._transcriber(pcm)).strip()
        import mlx_whisper

        import numpy as np
        samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
        result = mlx_whisper.transcribe(
            samples,
            path_or_hf_repo=ASR_MODEL_ID,
            language="en",
            verbose=False,
            condition_on_previous_text=False,
        )
        return str(result.get("text", "")).strip()


def quality_summary(quality: dict) -> dict:
    return {key: value for key, value in quality.items() if key != "samples"}
