import test from 'node:test';
import assert from 'node:assert/strict';
import { identityView } from '../src/identity/identity-view.js';
import type { IdentitySnapshot } from '../src/auth/authorization.js';
const locked: IdentitySnapshot = { state:'LOCKED', sessionId:null, expiresAt:null, provider:null, simulated:false };
test('identity display reason comes from backend guard and active session', () => {
  assert.equal(identityView(locked, {valid:false, reason:'Polar PPG identity factor is expired.'}, false, null).reason, 'Polar PPG identity factor is expired.');
  assert.equal(identityView(locked, {valid:true, reason:'fresh'}, false, null).reason, 'No active authenticated identity session.');
  assert.equal(identityView({...locked,state:'AUTHENTICATED'}, {valid:true, reason:'fresh'}, false, 'now').lastAuthenticatedAt, 'now');
  assert.match(identityView(locked, {valid:false, reason:'locked'}, true, null).reason, /simulation/);
});
