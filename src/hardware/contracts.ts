/** Adapter contracts only. No device APIs are called until a verified adapter is configured. */
export type ConnectionStatus = 'connected' | 'disconnected' | 'unknown';
export type VexoEvent =
  | { kind: 'microphone-audio'; receivedAt: string; payload: Uint8Array }
  | { kind: 'motion'; receivedAt: string; accel?: [number, number, number]; gyro?: [number, number, number] }
  | { kind: 'tap'; receivedAt: string; count: number };
export type PolarPpgSample = { receivedAt: string; samples: number[]; quality?: number };
export type IdentityVerificationResult = {
  verified: boolean;
  provider: string;
  simulated: boolean;
  reason: string;
};

export interface VexoAdapter {
  status(): ConnectionStatus;
  onEvent(listener: (event: VexoEvent) => void): () => void;
  disconnect(): Promise<void>;
}
export interface PolarAdapter {
  status(): ConnectionStatus;
  onPpg(listener: (sample: PolarPpgSample) => void): () => void;
  disconnect(): Promise<void>;
}
export interface IdentityProvider {
  readonly name: string;
  readonly simulated: boolean;
  verify(): Promise<IdentityVerificationResult>;
}
export interface PresenceMonitor {
  start(onLost: (reason: string) => void): Promise<void>;
  stop(): Promise<void>;
}

/** Development-only provider. Calling authenticate is an explicit simulation, not biometrics. */
export class SimulatedIdentityProvider implements IdentityProvider {
  readonly name = 'Development simulation';
  readonly simulated = true;
  async verify(): Promise<IdentityVerificationResult> {
    return { verified: true, provider: this.name, simulated: true, reason: 'Manually activated in the Clasp dashboard.' };
  }
}

export const hardwareState = {
  vexo: { status: 'disconnected' as ConnectionStatus, detail: 'No adapter configured' },
  polar: { status: 'disconnected' as ConnectionStatus, detail: 'No adapter configured; PPG matching is not implemented' },
};
