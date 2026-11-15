import json
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from speaker_verifier import LocalSpeakerVerifier, VoiceQualityError, speech_quality


def pcm_for(value: float, seconds: float = 5.0) -> bytes:
    count = int(16_000 * seconds)
    samples = np.full(count, int(value * 32767), dtype="<i2")
    return samples.tobytes()


def embed(samples):
    return ([1.0, 0.0] if float(np.mean(samples)) >= 0 else [0.0, 1.0]) + [0.0] * 190


class SpeakerVerifierTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.now = 1_800_000_000
        self.verifier = LocalSpeakerVerifier(
            self.root / "voice-profile.json", self.root / "models",
            embedder=embed, transcriber=lambda pcm: "a local test transcript", now=lambda: self.now,
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_registration_persists_embedding_locally_and_refuses_silent_replacement(self):
        profile = self.verifier.register(pcm_for(0.1))
        identity_id = profile["identityId"]
        self.assertEqual(identity_id, "primary-user")
        self.assertTrue(profile["profileId"])
        stored = json.loads((self.root / "voice-profile.json").read_text())
        self.assertEqual(stored["identityId"], identity_id)
        self.assertEqual(len(stored["embedding"]), 192)
        with self.assertRaises(FileExistsError):
            self.verifier.register(pcm_for(-0.1))
        replaced = self.verifier.register(pcm_for(-0.1), replace=True)
        self.assertEqual(replaced["identityId"], identity_id)
        self.assertNotEqual(replaced["profileId"], profile["profileId"])
        self.assertIsNotNone(replaced["reEnrolledAt"])

    def test_validation_matches_registered_speaker_and_returns_source_expiry_and_score(self):
        profile = self.verifier.register(pcm_for(0.1))
        result = self.verifier.validate(pcm_for(0.1), {"device": "Vexo-HACK-78", "sampleRate": 16000, "packetCount": 250})
        self.assertEqual(result["decision"], "MATCH")
        self.assertEqual(result["identityId"], profile["identityId"])
        self.assertEqual(result["similarity"], 1.0)
        self.assertEqual(result["audioSource"]["device"], "Vexo-HACK-78")
        self.assertEqual(result["expiresAt"], "9999-12-31T23:59:59Z")

    def test_different_speaker_embedding_is_a_mismatch(self):
        self.verifier.register(pcm_for(0.1))
        result = self.verifier.validate(pcm_for(-0.1), {"device": "Vexo-HACK-78"})
        self.assertEqual(result["decision"], "MISMATCH")
        self.assertIsNone(result["identityId"])
        self.assertEqual(result["similarity"], 0.0)

    def test_validation_never_changes_the_enrolled_profile(self):
        self.verifier.register(pcm_for(0.1))
        path = self.root / "voice-profile.json"
        before = path.read_bytes()
        self.verifier.validate(pcm_for(0.1), {"device": "Vexo-HACK-78"})
        self.verifier.validate(pcm_for(-0.1), {"device": "Vexo-HACK-78"})
        self.assertEqual(path.read_bytes(), before)

    def test_missing_profile_and_unusable_audio_are_uncertain(self):
        missing = self.verifier.validate(pcm_for(0.1), {"device": "Vexo-HACK-78"})
        self.assertEqual(missing["decision"], "UNCERTAIN")
        self.assertIsNone(missing["identityId"])
        self.assertIn("No registered", missing["reason"])
        with self.assertRaises(VoiceQualityError):
            speech_quality(pcm_for(0.0))
        self.verifier.register(pcm_for(0.1))
        unusable = self.verifier.validate(pcm_for(0.0), {"device": "Vexo-HACK-78"})
        self.assertEqual(unusable["decision"], "UNCERTAIN")
        self.assertFalse(unusable["audioUsable"])
        self.assertIsNone(unusable["identityId"])

    def test_legacy_profile_requires_explicit_new_enrollment(self):
        path = self.root / "voice-profile.json"
        old = {"schemaVersion": 1, "identityId": "legacy-uuid", "model": "speechbrain/spkrec-ecapa-voxceleb", "embedding": embed(np.array([1.])), "enrolledAt": "2020-01-01T00:00:00Z"}
        path.write_text(json.dumps(old))
        before = path.read_bytes()
        self.assertTrue(self.verifier.profile_metadata()["reEnrollmentRequired"])
        result = self.verifier.validate(pcm_for(0.1), {"device": "Vexo-HACK-78"})
        self.assertEqual(result["decision"], "UNCERTAIN")
        self.assertIsNone(result["identityId"])
        self.assertEqual(path.read_bytes(), before)
        with self.assertRaises(FileExistsError):
            self.verifier.register(pcm_for(0.1))
        self.assertEqual(path.read_bytes(), before)
        enrolled = self.verifier.register(pcm_for(0.1), replace=True)
        self.assertEqual(enrolled["identityId"], "primary-user")
        self.assertEqual(self.verifier.validate(pcm_for(0.1), {})["identityId"], "primary-user")

    def test_corrupt_existing_profile_is_not_silently_overwritten(self):
        path = self.root / "voice-profile.json"
        path.write_text("not json")
        self.assertTrue(self.verifier.profile_metadata()["reEnrollmentRequired"])
        with self.assertRaises(FileExistsError):
            self.verifier.register(pcm_for(0.1))
        self.assertEqual(path.read_text(), "not json")


if __name__ == "__main__":
    unittest.main()
