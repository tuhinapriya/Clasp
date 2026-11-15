import Stripe from 'stripe';
import { randomUUID } from 'node:crypto';
import { AsyncLocalStorage } from 'node:async_hooks';
import { publish } from '../events.js';
import { AuthorizationService } from '../auth/authorization.js';

export const requestScope = new AsyncLocalStorage<{ requestId: string; demo?: boolean; isStillValid?: () => boolean; getEvidence?: () => unknown }>();
export interface PaymentGateway {
  create(input: { amount: number; currency: string; description: string; metadata: Record<string, string> }, idempotencyKey: string): Promise<{ id: string; status: string; livemode: boolean; amount: number; currency: string; amount_received: number }>;
}
export type PaymentOutcome = {
  ok: boolean;
  decision: 'ALLOW' | 'DENY';
  reason: string;
  stripeCalled: boolean;
  paymentIntent?: { id: string; status: string; livemode: boolean; amount: number; currency: string; amountReceived: number };
};

export class StripeTestGateway implements PaymentGateway {
  private client: Stripe;
  constructor(secretKey = process.env.STRIPE_SECRET_KEY) {
    if (!secretKey || !secretKey.startsWith('sk_test_')) throw new Error('Stripe test-mode credentials are missing. Set STRIPE_SECRET_KEY to an sk_test_ key.');
    this.client = new Stripe(secretKey);
  }
  async create(input: { amount: number; currency: string; description: string; metadata: Record<string, string> }, idempotencyKey: string) {
    const result = await this.client.paymentIntents.create({
      amount: input.amount,
      currency: input.currency,
      description: input.description,
      metadata: input.metadata,
      payment_method: 'pm_card_visa',
      confirm: true,
      automatic_payment_methods: { enabled: true, allow_redirects: 'never' },
    }, { idempotencyKey });
    return { id: result.id, status: result.status, livemode: result.livemode, amount: result.amount, currency: result.currency, amount_received: result.amount_received };
  }
}

export class ProtectedPaymentTool {
  private outcomes = new Map<string, Promise<PaymentOutcome>>();
  constructor(private readonly auth: AuthorizationService, private readonly gatewayFactory: () => PaymentGateway = () => new StripeTestGateway()) {}

  async execute(input: { amountUsd: number; merchantId: string }): Promise<PaymentOutcome> {
    const requestId = requestScope.getStore()?.requestId ?? randomUUID();
    const prior = this.outcomes.get(requestId);
    if (prior) return prior;
    const run = this.runOnce(input, requestId);
    this.outcomes.set(requestId, run);
    if (this.outcomes.size > 500) this.outcomes.delete(this.outcomes.keys().next().value!);
    return run;
  }

  private deny(requestId: string, requestedAction: Record<string, unknown>, identityState: string, reason: string) {
    const outcome: PaymentOutcome = { ok: false, decision: 'DENY', reason, stripeCalled: false };
    publish('audit.entry', { requestId, requestedAction, identityState, decision: 'DENY', reason, executionOutcome: 'blocked', externalApiInvoked: false, source: 'biometric' });
    publish('tool.execution.completed', { requestId, tool: 'create_test_payment', ...outcome });
    return outcome;
  }

  private async runOnce(input: { amountUsd: number; merchantId: string }, requestId: string): Promise<PaymentOutcome> {
    const args = { amountCents: Math.round(input.amountUsd * 100), merchantId: input.merchantId, currency: process.env.STRIPE_CURRENCY ?? 'usd' };
    publish('agent.tool.requested', { requestId, tool: 'create_test_payment', requestedAction: { amountUsd: input.amountUsd, merchantId: input.merchantId, currency: args.currency } });

    const requestedAction = { tool: 'create_test_payment', amountUsd: input.amountUsd, merchantId: input.merchantId };
    const scope = requestScope.getStore();
    if (scope?.demo) {
      publish('demo.boundary', { requestId, simulated: false, biometrics: scope.getEvidence?.() });
      await new Promise(resolve => setTimeout(resolve, 1000));
    }
    if (scope?.isStillValid && !scope.isStillValid()) return this.deny(requestId, requestedAction, 'LOCKED', 'Vexo disconnected before authorization.');
    const authorize = (phase: 'preflight' | 'execution') => this.auth.authorize('create_test_payment', args, phase, requestId);
    const preflight = authorize('preflight');
    if (!preflight.allowed) return this.deny(requestId, requestedAction, preflight.identityState, preflight.reason);

    // Re-check policy and current biometric evidence immediately before the external side effect.
    if (scope?.isStillValid && !scope.isStillValid()) return this.deny(requestId, requestedAction, 'LOCKED', 'Vexo disconnected before execution.');
    const decision = authorize('execution');
    if (!decision.allowed) return this.deny(requestId, requestedAction, decision.identityState, decision.reason);

    let gateway: PaymentGateway;
    try {
      gateway = this.gatewayFactory();
    } catch (error) {
      const reason = error instanceof Error ? error.message : 'Stripe test mode is not configured.';
      return this.deny(requestId, requestedAction, decision.identityState, reason);
    }

    try {
      const result = await gateway.create({
        amount: args.amountCents,
        currency: args.currency,
        description: 'Clasp demo payment (Stripe test mode)',
        metadata: { clasp_request_id: requestId, merchant_id: input.merchantId, demo: 'true' },
      }, `clasp-${requestId}`);
      if (result.livemode) throw new Error('Stripe returned a live-mode PaymentIntent; refusing to report it as a demo payment.');
      const outcome: PaymentOutcome = { ok: result.status === 'succeeded', decision: 'ALLOW', reason: result.status === 'succeeded' ? decision.reason : `Stripe test payment status: ${result.status}.`, stripeCalled: true, paymentIntent: { id: result.id, status: result.status, livemode: false, amount: result.amount, currency: result.currency, amountReceived: result.amount_received } };
      publish('audit.entry', { requestId, requestedAction, identityState: decision.identityState, decision: 'ALLOW', reason: decision.reason, executionOutcome: result.status, externalApiInvoked: true, paymentIntentId: result.id, source: 'biometric' });
      publish('tool.execution.completed', { requestId, tool: 'create_test_payment', ...outcome });
      return outcome;
    } catch (error) {
      const reason = error instanceof Error ? error.message : 'Stripe test request failed.';
      const outcome: PaymentOutcome = { ok: false, decision: 'ALLOW', reason, stripeCalled: true };
      publish('audit.entry', { requestId, requestedAction, identityState: decision.identityState, decision: 'ALLOW', reason, executionOutcome: 'stripe-error', externalApiInvoked: true });
      publish('tool.execution.completed', { requestId, tool: 'create_test_payment', ...outcome });
      return outcome;
    }
  }
}
