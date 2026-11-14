import type { IdentitySnapshot } from '../auth/authorization.js';

/** Presentation text follows the server's authorization state and guard. */
export function identityView(identity: IdentitySnapshot, biometricStatus: { valid: boolean; reason: string }, simulatedMode: boolean, lastAuthenticatedAt: string | null) {
  const reason = simulatedMode
    ? 'Development simulation is active; biometric authorization is separate.'
    : !biometricStatus.valid ? biometricStatus.reason
    : identity.state === 'AUTHENTICATED' ? 'Fresh voice and PPG matches verified.'
    : 'No active authenticated identity session.';
  return { reason, lastAuthenticatedAt };
}
