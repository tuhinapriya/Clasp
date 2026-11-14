import { publish } from '../events.js';
import type { PolarPpgSample } from '../hardware/contracts.js';
import type { IdentityProvider, IdentityVerificationResult } from '../hardware/contracts.js';

export type BiometricFactor = 'voice-speaker' | 'polar-ppg';
export type MatchDecision = 'MATCH' | 'MISMATCH' | 'UNCERTAIN';

/** Result emitted only by a real verifier adapter. Raw sensor signals are never results. */
export type FactorVerification = {
  factor: BiometricFactor;
  decision: MatchDecision;
  subjectId: string | null;
  verifiedAt: string;
  expiresAt: string;
  verifier: string;
  simulated: false;
  similarity?: number | null;
  identityId?: string | null;
  audioSource?: Record<string, string | number | boolean | null>;
};

export type FactorStatus = 'UNAVAILABLE' | MatchDecision | 'EXPIRED';
export type BiometricSnapshot = {
  voiceSpeaker: { status: FactorStatus; verifier: string | null; verifiedAt: string | null; expiresAt: string | null; similarity: number | null; identityId: string | null; audioSource: Record<string, string | number | boolean | null> | null };
  polarPpg: { status: FactorStatus; verifier: string | null; verifiedAt: string | null; expiresAt: string | null };
  matchedSubject: boolean;
  authorized: boolean;
};

/**
 * Integration contracts for verifier modules. Vexo connection, audio presence,
 * transcription, and Polar pulse detection do not implement these interfaces.
 */
export interface VoiceSpeakerVerifier {
  start(handlers: { onResult: (result: FactorVerification) => void; onStatus: (status: 'ready' | 'unavailable', reason?: string) => void }): Promise<void>;
  consumeVexoAudio(packet: Uint8Array, sequence: number): Promise<void>;
  stop(): Promise<void>;
}

export interface PolarPpgIdentityVerifier {
  start(handlers: { onResult: (result: FactorVerification) => void; onStatus: (status: 'ready' | 'unavailable', reason?: string) => void }): Promise<void>;
  consumePpgSample(sample: PolarPpgSample): Promise<void>;
  stop(): Promise<void>;
}

/** Bind a separately implemented verifier to the shared fail-closed evidence store. */
export async function attachVoiceSpeakerVerifier(verifier: VoiceSpeakerVerifier, evidence: BiometricEvidenceService) {
  let ready = false;
  await verifier.start({
    onResult: (result) => result.factor === 'voice-speaker'
      ? evidence.record(result)
      : evidence.invalidate('voice-speaker', 'Voice verifier returned a result for the wrong factor.'),
    onStatus: (status, reason) => {
      ready = status === 'ready';
      if (!ready) evidence.setUnavailable('voice-speaker', reason ?? 'Voice verifier is unavailable.');
    },
  });
  return {
    consumeVexoAudio: (packet: Uint8Array, sequence: number) => ready ? verifier.consumeVexoAudio(packet, sequence) : Promise.resolve(),
    disconnect: async () => {
      ready = false;
      evidence.setUnavailable('voice-speaker', 'Voice verifier disconnected.');
      await verifier.stop();
    },
  };
}

/** Bind the separate Polar/PaPaGei implementation; PPG samples alone never authenticate. */
export async function attachPolarPpgVerifier(verifier: PolarPpgIdentityVerifier, evidence: BiometricEvidenceService) {
  let ready = false;
  await verifier.start({
    onResult: (result) => result.factor === 'polar-ppg'
      ? evidence.record(result)
      : evidence.invalidate('polar-ppg', 'Polar verifier returned a result for the wrong factor.'),
    onStatus: (status, reason) => {
      ready = status === 'ready';
      if (!ready) evidence.setUnavailable('polar-ppg', reason ?? 'Polar identity verifier is unavailable.');
    },
  });
  return {
    consumePpgSample: (sample: PolarPpgSample) => ready ? verifier.consumePpgSample(sample) : Promise.resolve(),
    disconnect: async () => {
      ready = false;
      evidence.setUnavailable('polar-ppg', 'Polar identity verifier disconnected.');
      await verifier.stop();
    },
  };
}

type StoredResult = FactorVerification & { verifiedAtMs: number; expiresAtMs: number };
const MAX_RESULT_FRESHNESS_MS = 15_000;
const MAX_RESULT_TTL_MS = 30_000;

export class BiometricEvidenceService {
  private results = new Map<BiometricFactor, StoredResult>();
  private availability: Record<BiometricFactor, string | null> = { 'voice-speaker': null, 'polar-ppg': null };
  private listeners = new Set<(snapshot: BiometricSnapshot, reason: string) => void>();

  constructor(private readonly now = () => Date.now()) {}

  record(result: FactorVerification) {
    const verifiedAtMs = Date.parse(result.verifiedAt);
    const expiresAtMs = Date.parse(result.expiresAt);
    const now = this.now();
    if (!['voice-speaker', 'polar-ppg'].includes(result.factor)
      || !['MATCH', 'MISMATCH', 'UNCERTAIN'].includes(result.decision)
      || result.simulated !== false || typeof result.verifier !== 'string' || !result.verifier.trim()
      || !Number.isFinite(verifiedAtMs) || !Number.isFinite(expiresAtMs)
      || verifiedAtMs > now || expiresAtMs <= now || expiresAtMs <= verifiedAtMs
      || expiresAtMs - verifiedAtMs > MAX_RESULT_TTL_MS
      || (result.decision === 'MATCH' && !result.subjectId?.trim())) {
      this.invalidate(result.factor, 'Biometric verification result was malformed, simulated, or outside its validity window.');
      return false;
    }
    this.results.set(result.factor, { ...result, verifiedAtMs, expiresAtMs });
    this.availability[result.factor] = null;
    this.notify(`${result.factor} verifier returned ${result.decision}.`);
    return true;
  }

  setUnavailable(factor: BiometricFactor, reason: string) {
    this.results.delete(factor);
    this.availability[factor] = reason || 'Verifier unavailable.';
    this.notify(`${factor} verifier unavailable.`);
  }

  invalidate(factor: BiometricFactor, reason: string) {
    const changed = this.results.delete(factor) || this.availability[factor] !== reason;
    this.availability[factor] = reason;
    if (changed) this.notify(reason);
  }

  onChange(listener: (snapshot: BiometricSnapshot, reason: string) => void) {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  snapshot(): BiometricSnapshot {
    const voice = this.factorSnapshot('voice-speaker');
    const polar = this.factorSnapshot('polar-ppg');
    const voiceResult = this.results.get('voice-speaker');
    const voiceSpeaker = {
      ...voice,
      similarity: voiceResult?.similarity ?? null,
      identityId: voiceResult?.identityId ?? null,
      audioSource: voiceResult?.audioSource ?? null,
    };
    const polarResult = this.freshResult('polar-ppg');
    const freshVoiceResult = this.freshResult('voice-speaker');
    const matchedSubject = freshVoiceResult?.subjectId === 'primary-user' && polarResult?.subjectId === 'primary-user';
    return {
      voiceSpeaker,
      polarPpg: polar,
      matchedSubject,
      authorized: matchedSubject && voice.status === 'MATCH' && polar.status === 'MATCH',
    };
  }

  authorizationStatus() {
    const snapshot = this.snapshot();
    if (!snapshot.authorized) return { valid: false, reason: this.denialReason(snapshot) };
    return { valid: true, reason: 'Fresh voice-speaker and Polar PPG identity matches agree.' };
  }

  private factorSnapshot(factor: BiometricFactor) {
    const result = this.results.get(factor);
    if (!result) return { status: 'UNAVAILABLE' as const, verifier: null, verifiedAt: null, expiresAt: null };
    if (this.now() - result.verifiedAtMs > MAX_RESULT_FRESHNESS_MS || this.now() >= result.expiresAtMs) {
      return { status: 'EXPIRED' as const, verifier: result.verifier, verifiedAt: result.verifiedAt, expiresAt: result.expiresAt };
    }
    return { status: result.decision, verifier: result.verifier, verifiedAt: result.verifiedAt, expiresAt: result.expiresAt };
  }

  private freshResult(factor: BiometricFactor) {
    const result = this.results.get(factor);
    if (!result || this.factorSnapshot(factor).status !== 'MATCH') return null;
    return result;
  }

  private denialReason(snapshot: BiometricSnapshot) {
    if (snapshot.voiceSpeaker.status !== 'MATCH') return `Voice speaker factor is ${snapshot.voiceSpeaker.status.toLowerCase()}.`;
    if (snapshot.polarPpg.status !== 'MATCH') return `Polar PPG identity factor is ${snapshot.polarPpg.status.toLowerCase()}.`;
    return 'Biometric factors did not verify the same identity.';
  }

  private notify(reason: string) {
    const snapshot = this.snapshot();
    publish('identity.biometric_factors', {
      voiceSpeaker: snapshot.voiceSpeaker.status,
      polarPpg: snapshot.polarPpg.status,
      matchedSubject: snapshot.matchedSubject,
      authorized: snapshot.authorized,
      reason,
    });
    for (const listener of this.listeners) listener(snapshot, reason);
  }
}

export class BiometricIdentityProvider implements IdentityProvider {
  readonly name = 'Vexo voice speaker + Polar PPG';
  readonly simulated = false;
  constructor(private readonly evidence: BiometricEvidenceService) {}

  async verify(): Promise<IdentityVerificationResult> {
    const status = this.evidence.authorizationStatus();
    return { verified: status.valid, provider: this.name, simulated: false, reason: status.reason };
  }
}
