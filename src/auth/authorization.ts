import { publish } from '../events.js';
import type { IdentityProvider } from '../hardware/contracts.js';

export type IdentityState = 'LOCKED' | 'AUTHENTICATED' | 'EXPIRED';
export type IdentitySnapshot = {
  state: IdentityState;
  sessionId: string | null;
  expiresAt: string | null;
  provider: string | null;
  simulated: boolean;
};
export type AuthDecision = {
  allowed: boolean;
  reason: string;
  identityState: IdentityState;
  sessionId: string | null;
  checkedAt: string;
};
export type PaymentArgs = { amountCents: number; merchantId: string; currency: string };
export type SessionGuard = () => { valid: boolean; reason: string };
type Policy = (args: PaymentArgs) => string | null;

const policies: Record<string, Policy> = {
  create_test_payment: (args) => {
    if (!Number.isInteger(args.amountCents) || args.amountCents < 1) return 'Amount must be a positive integer number of cents.';
    const max = Number(process.env.MAX_TEST_PAYMENT_CENTS ?? 1000);
    if (args.amountCents > max) return `Amount exceeds the $${(max / 100).toFixed(2)} demo limit.`;
    const merchant = process.env.CLASP_DEMO_MERCHANT_ID ?? 'clasp_demo_merchant';
    if (args.merchantId !== merchant) return 'Merchant is not on the Clasp demo allowlist.';
    if (args.currency !== 'usd') return 'Only USD is enabled for the demo merchant.';
    return null;
  },
};

export class AuthorizationService {
  private state: IdentityState = 'LOCKED';
  private sessionId: string | null = null;
  private expiresAtMs: number | null = null;
  private providerName: string | null = null;
  private simulated = false;

  constructor(
    private readonly provider: IdentityProvider,
    private readonly ttlSeconds = Number(process.env.SESSION_TTL_SECONDS ?? 120),
    private readonly now = () => Date.now(),
    private readonly sessionGuard?: SessionGuard,
  ) {}

  snapshot(): IdentitySnapshot {
    this.expireIfNeeded();
    this.enforceCurrentIdentity();
    return { state: this.state, sessionId: this.sessionId, expiresAt: this.expiresAtMs ? new Date(this.expiresAtMs).toISOString() : null, provider: this.providerName, simulated: this.simulated };
  }

  async authenticate() {
    const proof = await this.provider.verify();
    if (!proof.verified) {
      this.revoke(proof.reason || 'Identity verification failed.');
      return this.snapshot();
    }
    this.state = 'AUTHENTICATED';
    this.sessionId = crypto.randomUUID();
    this.expiresAtMs = this.now() + this.ttlSeconds * 1000;
    this.providerName = proof.provider;
    this.simulated = proof.simulated;
    const snapshot = this.snapshot();
    publish('identity.authenticated', { ...snapshot, reason: proof.reason });
    return snapshot;
  }

  revoke(reason = 'Identity session revoked by user.') {
    const previousSessionId = this.sessionId;
    this.state = 'LOCKED';
    this.sessionId = null;
    this.expiresAtMs = null;
    this.providerName = null;
    this.simulated = false;
    publish('identity.revoked', { previousSessionId, reason });
    return this.snapshot();
  }

  authorize(toolName: string, args: PaymentArgs, phase: 'preflight' | 'execution' = 'execution', requestId?: string): AuthDecision {
    this.expireIfNeeded();
    this.enforceCurrentIdentity();
    const checkedAt = new Date(this.now()).toISOString();
    const identityState = this.state;
    let reason = '';
    if (!(toolName in policies)) reason = 'No authorization policy exists for this tool.';
    else if (identityState !== 'AUTHENTICATED' || !this.sessionId) {
      const guard = this.sessionGuard?.();
      reason = guard && !guard.valid ? guard.reason : identityState === 'EXPIRED' ? 'Authentication session expired.' : 'No active authenticated identity session.';
    }
    else reason = policies[toolName]!(args) ?? '';
    const decision: AuthDecision = { allowed: !reason, reason: reason || 'Authenticated session and tool policy passed.', identityState, sessionId: this.sessionId, checkedAt };
    publish('authorization.decision', { requestId, phase, tool: toolName, requestedAction: { amountCents: args.amountCents, merchantId: args.merchantId, currency: args.currency }, ...decision, simulated: this.simulated });
    return decision;
  }

  private expireIfNeeded() {
    if (this.state === 'AUTHENTICATED' && this.expiresAtMs !== null && this.now() >= this.expiresAtMs) {
      this.state = 'EXPIRED';
      const expiredSessionId = this.sessionId;
      this.sessionId = null;
      publish('identity.expired', { expiredSessionId, reason: 'Authentication session expired.' });
    }
  }

  private enforceCurrentIdentity() {
    if (this.state !== 'AUTHENTICATED' || !this.sessionGuard) return;
    const result = this.sessionGuard();
    if (!result.valid) this.revoke(result.reason || 'Current identity verification is uncertain.');
  }
}
