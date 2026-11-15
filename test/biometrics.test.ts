import test from 'node:test';
import assert from 'node:assert/strict';
import { AuthorizationService } from '../src/auth/authorization.js';
import { BiometricEvidenceService, BiometricIdentityProvider, type BiometricFactor, type FactorVerification } from '../src/identity/biometrics.js';
import { SimulatedIdentityProvider } from '../src/hardware/contracts.js';
import { ProtectedPaymentTool, requestScope, type PaymentGateway } from '../src/payments/test-payment.js';

const args = { amountCents: 500, merchantId: 'clasp_demo_merchant', currency: 'usd' };
const flush = () => new Promise<void>((resolve) => setImmediate(resolve));

test('voice evidence remains available until explicitly invalidated while stale PPG expires', () => {
  let now = 10_000;
  const evidence = new BiometricEvidenceService(() => now);
  assert.equal(evidence.record({ factor: 'voice-speaker', decision: 'MATCH', subjectId: 'primary-user', verifier: 'voice', verifiedAt: new Date(now).toISOString(), expiresAt: '9999-12-31T23:59:59Z', simulated: false }), true);
  assert.equal(evidence.record({ factor: 'polar-ppg', decision: 'MATCH', subjectId: 'primary-user', verifier: 'polar', verifiedAt: new Date(now).toISOString(), expiresAt: new Date(now + 15_000).toISOString(), simulated: false }), true);
  now += 60_000;
  assert.equal(evidence.snapshot().voiceSpeaker.status, 'MATCH');
  assert.equal(evidence.snapshot().polarPpg.status, 'EXPIRED');
  evidence.invalidate('voice-speaker', 'Vexo disconnected');
  assert.equal(evidence.snapshot().voiceSpeaker.status, 'UNAVAILABLE');
});

function setup() {
  let now = 10_000;
  const evidence = new BiometricEvidenceService(() => now);
  const auth = new AuthorizationService(
    new BiometricIdentityProvider(evidence), 120, () => now, () => evidence.authorizationStatus(),
  );
  evidence.onChange((snapshot, reason) => {
    const session = auth.snapshot();
    if (snapshot.authorized && session.state !== 'AUTHENTICATED') void auth.authenticate();
    else if (!snapshot.authorized && session.state === 'AUTHENTICATED' && !session.simulated) auth.revoke(reason);
  });
  const record = (factor: BiometricFactor, decision: FactorVerification['decision'] = 'MATCH', subjectId: string | null = 'primary-user') => evidence.record({
    factor, decision, subjectId, verifier: `${factor}-test-verifier`,
    verifiedAt: new Date(now).toISOString(), expiresAt: new Date(now + 10_000).toISOString(), simulated: false,
  });
  return { evidence, auth, record, advance: (ms: number) => { now += ms; } };
}

test('automatically authenticates when fresh voice and Polar matches agree on the same identity', async () => {
  const { evidence, auth, record } = setup();
  record('voice-speaker');
  assert.equal(auth.authorize('create_test_payment', args).allowed, false);
  record('polar-ppg');
  await flush();
  assert.equal(evidence.snapshot().authorized, true);
  assert.equal(auth.snapshot().state, 'AUTHENTICATED');
  assert.equal(auth.snapshot().simulated, false);
  assert.equal(auth.authorize('create_test_payment', args).allowed, true);
});

test('a single factor never authenticates or authorizes payment', async () => {
  const { auth, record } = setup();
  record('voice-speaker');
  await flush();
  assert.equal(auth.snapshot().state, 'LOCKED');
  assert.equal(auth.authorize('create_test_payment', args).allowed, false);
});

test('both factors automatically authorize the protected payment tool without a gesture gate', async () => {
  const { auth, record } = setup();
  record('voice-speaker');
  record('polar-ppg');
  await flush();
  let calls = 0;
  const gateway: PaymentGateway = { create: async () => {
    calls++;
    return { id: 'pi_biometric_test', status: 'succeeded', livemode: false, amount: 500, currency: 'usd', amount_received: 500 };
  } };
  const payments = new ProtectedPaymentTool(auth, () => gateway);
  const outcome = await requestScope.run({ requestId: 'biometric-allow-001' }, () => payments.execute({ amountUsd: 5, merchantId: 'clasp_demo_merchant' }));
  assert.equal(outcome.decision, 'ALLOW');
  assert.equal(outcome.paymentIntent?.id, 'pi_biometric_test');
  assert.equal(calls, 1);
});

test('a single biometric factor cannot reach the protected payment gateway', async () => {
  const { auth, record } = setup();
  record('voice-speaker');
  await flush();
  let calls = 0;
  const gateway: PaymentGateway = { create: async () => {
    calls++;
    return { id: 'pi_should_not_exist', status: 'succeeded', livemode: false, amount: 500, currency: 'usd', amount_received: 500 };
  } };
  const payments = new ProtectedPaymentTool(auth, () => gateway);
  const outcome = await requestScope.run({ requestId: 'biometric-deny-001' }, () => payments.execute({ amountUsd: 5, merchantId: 'clasp_demo_merchant' }));
  assert.equal(outcome.decision, 'DENY');
  assert.equal(outcome.stripeCalled, false);
  assert.equal(calls, 0);
});

test('expired biometric evidence immediately locks and denies', async () => {
  const { auth, record, advance } = setup();
  record('voice-speaker');
  record('polar-ppg');
  await flush();
  assert.equal(auth.snapshot().state, 'AUTHENTICATED');
  advance(15_001);
  assert.equal(auth.authorize('create_test_payment', args).allowed, false);
  assert.equal(auth.snapshot().state, 'LOCKED');
});

test('disconnecting either factor revokes biometric authorization', async () => {
  const { evidence, auth, record } = setup();
  record('voice-speaker');
  record('polar-ppg');
  await flush();
  assert.equal(auth.snapshot().state, 'AUTHENTICATED');
  evidence.setUnavailable('polar-ppg', 'Polar verifier disconnected.');
  assert.equal(auth.authorize('create_test_payment', args).allowed, false);
  assert.equal(auth.snapshot().state, 'LOCKED');
});

test('mismatched or uncertain factor results deny authorization', async () => {
  const mismatch = setup();
  mismatch.record('voice-speaker', 'MATCH', 'primary-user');
  mismatch.record('polar-ppg', 'MATCH', 'another-person');
  await flush();
  assert.equal(mismatch.auth.snapshot().state, 'LOCKED');
  assert.equal(mismatch.auth.authorize('create_test_payment', args).allowed, false);

  const uncertain = setup();
  uncertain.record('voice-speaker');
  uncertain.record('polar-ppg', 'UNCERTAIN');
  await flush();
  assert.equal(uncertain.auth.snapshot().state, 'LOCKED');
  assert.equal(uncertain.auth.authorize('create_test_payment', args).allowed, false);
});

test('fresh matches do not bypass the payment policy limit', async () => {
  const { auth, record } = setup();
  record('voice-speaker');
  record('polar-ppg');
  await flush();
  assert.equal(auth.snapshot().state, 'AUTHENTICATED');
  const decision = auth.authorize('create_test_payment', { ...args, amountCents: 1001 });
  assert.equal(decision.allowed, false);
  assert.match(decision.reason, /exceeds/);
});

test('simulated factor claims cannot be recorded as biometric matches', () => {
  const { evidence, record } = setup();
  record('voice-speaker');
  const accepted = evidence.record({
    factor: 'polar-ppg', decision: 'MATCH', subjectId: 'owner-1', verifier: 'test',
    verifiedAt: new Date(10_000).toISOString(), expiresAt: new Date(20_000).toISOString(), simulated: true,
  } as unknown as FactorVerification);
  assert.equal(accepted, false);
  assert.equal(evidence.snapshot().authorized, false);
});

test('development simulation is explicitly simulated and independent from biometric evidence', async () => {
  const evidence = new BiometricEvidenceService();
  const auth = new AuthorizationService(new SimulatedIdentityProvider());
  await auth.authenticate();
  assert.equal(auth.snapshot().simulated, true);
  assert.equal(evidence.snapshot().authorized, false);
  assert.equal(auth.authorize('create_test_payment', args).allowed, true);
});


test('matching legacy UUIDs do not authorize the canonical identity', () => {
  const { evidence, record } = setup();
  record('voice-speaker', 'MATCH', 'legacy-uuid');
  record('polar-ppg', 'MATCH', 'legacy-uuid');
  assert.equal(evidence.snapshot().authorized, false);
});
