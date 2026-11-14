import test from 'node:test';
import assert from 'node:assert/strict';
import { AuthorizationService } from '../src/auth/authorization.js';
import type { IdentityProvider } from '../src/hardware/contracts.js';

const verified: IdentityProvider = { name: 'test provider', simulated: true, verify: async () => ({ verified: true, provider: 'test provider', simulated: true, reason: 'test' }) };
const args = { amountCents: 500, merchantId: 'clasp_demo_merchant', currency: 'usd' };

test('defaults to LOCKED and denies missing identity', () => {
  const auth = new AuthorizationService(verified, 120);
  assert.equal(auth.snapshot().state, 'LOCKED');
  const decision = auth.authorize('create_test_payment', args);
  assert.equal(decision.allowed, false);
  assert.match(decision.reason, /No active authenticated/);
});

test('authenticates for a short session and expires closed', async () => {
  let now = 1_000;
  const auth = new AuthorizationService(verified, 2, () => now);
  await auth.authenticate();
  assert.equal(auth.authorize('create_test_payment', args).allowed, true);
  now += 2_000;
  assert.equal(auth.snapshot().state, 'EXPIRED');
  assert.equal(auth.authorize('create_test_payment', args).allowed, false);
});

test('revokes immediately and enforces tool-specific limits', async () => {
  const auth = new AuthorizationService(verified, 120);
  await auth.authenticate();
  assert.equal(auth.authorize('other_tool', args).allowed, false);
  assert.equal(auth.authorize('create_test_payment', { ...args, amountCents: 501 }).allowed, false);
  auth.revoke();
  assert.equal(auth.authorize('create_test_payment', args).allowed, false);
});
