import test from 'node:test';
import assert from 'node:assert/strict';
import { AuthorizationService } from '../src/auth/authorization.js';
import type { IdentityProvider } from '../src/hardware/contracts.js';
import { ProtectedPaymentTool, requestScope, type PaymentGateway } from '../src/payments/test-payment.js';

const provider: IdentityProvider = { name: 'test provider', simulated: true, verify: async () => ({ verified: true, provider: 'test provider', simulated: true, reason: 'test' }) };
const result = { id: 'pi_test_123', status: 'succeeded', livemode: false, amount: 500, currency: 'usd', amount_received: 500 };

test('denied action never invokes payment gateway', async () => {
  const auth = new AuthorizationService(provider);
  let calls = 0;
  const gateway: PaymentGateway = { create: async () => { calls++; return result; } };
  const tool = new ProtectedPaymentTool(auth, () => gateway);
  const output = await requestScope.run({ requestId: 'denied-request-1' }, () => tool.execute({ amountUsd: 5, merchantId: 'clasp_demo_merchant' }));
  assert.equal(output.decision, 'DENY');
  assert.equal(output.stripeCalled, false);
  assert.equal(calls, 0);
});

test('authorized action reaches Stripe once and duplicate request is idempotent', async () => {
  const auth = new AuthorizationService(provider);
  await auth.authenticate();
  let calls = 0;
  const gateway: PaymentGateway = { create: async () => { calls++; return result; } };
  const tool = new ProtectedPaymentTool(auth, () => gateway);
  const run = () => requestScope.run({ requestId: 'allowed-request-1' }, () => tool.execute({ amountUsd: 5, merchantId: 'clasp_demo_merchant' }));
  const [first, duplicate] = await Promise.all([run(), run()]);
  assert.equal(first.ok, true);
  assert.equal(first.paymentIntent?.livemode, false);
  assert.deepEqual(duplicate, first);
  assert.equal(calls, 1);
});

test('an authorized ten-dollar test payment reaches the gateway for 1000 cents', async () => {
  const auth = new AuthorizationService(provider);
  await auth.authenticate();
  let amount = 0;
  const gateway: PaymentGateway = { create: async (input) => { amount = input.amount; return { ...result, amount: input.amount, amount_received: input.amount }; } };
  const output = await requestScope.run({ requestId: 'allowed-ten-dollar-test' }, () => new ProtectedPaymentTool(auth, () => gateway).execute({ amountUsd: 10, merchantId: 'clasp_demo_merchant' }));
  assert.equal(output.decision, 'ALLOW');
  assert.equal(amount, 1000);
});


test('StripeTestGateway refuses live credentials before any API call', async () => {
  const { StripeTestGateway } = await import('../src/payments/test-payment.js');
  assert.throws(() => new StripeTestGateway('sk_live_never_use'), /test-mode credentials/);
});
