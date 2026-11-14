const base = process.env.CLASP_URL ?? 'http://127.0.0.1:4173';
const id = () => globalThis.crypto.randomUUID();
async function post(path, body = {}) {
  const response = await fetch(`${base}${path}`, { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(body) });
  const result = await response.json();
  if (!response.ok) throw new Error(result.error ?? `Request failed: ${response.status}`);
  return result;
}
try {
  const health = await fetch(`${base}/api/state`);
  if (!health.ok) throw new Error(`Clasp server did not respond at ${base}`);
  console.log('SCENARIO A — authorized test payment');
  const session = await post('/api/identity/authenticate');
  console.log(`Simulated identity: ${session.state}; expires ${session.expiresAt}`);
  const allowed = await post('/api/agent/message', { text: 'Pay $5 to the demo merchant', requestId: id() });
  console.log(allowed.answer);
  console.log('');
  console.log('SCENARIO B — revoke, then request the same action');
  const revoked = await post('/api/identity/revoke');
  console.log(`Identity state: ${revoked.state}`);
  const denied = await post('/api/agent/message', { text: 'Pay $5 to the demo merchant', requestId: id() });
  console.log(denied.answer);
  console.log('');
  console.log('Open the dashboard for live audit events. With no STRIPE_SECRET_KEY, the authorized action is not sent to Stripe.');
} catch (error) {
  console.error(error instanceof Error ? error.message : error);
  console.error('Start the server with: npm run dev');
  process.exitCode = 1;
}
