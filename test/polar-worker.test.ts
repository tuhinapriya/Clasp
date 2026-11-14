import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, writeFileSync, chmodSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { BiometricEvidenceService, attachPolarPpgVerifier } from '../src/identity/biometrics.js';
import { PolarWorkerVerifier } from '../src/identity/polar-worker.js';

const pause = () => new Promise(resolve => setTimeout(resolve, 30));
async function setup(enroll = false) {
  const dir = mkdtempSync(path.join(tmpdir(), 'clasp-polar-test-'));
  const executable = path.join(dir, 'worker');
  writeFileSync(executable, '#!/bin/sh\nwhile IFS= read -r line; do printf "%s\\n" "$line"; done\n');
  chmodSync(executable, 0o700);
  const evidence = new BiometricEvidenceService();
  const events: Record<string, unknown>[] = [];
  const worker = new PolarWorkerVerifier({ python: executable, moduleDir: dir, profile: '', source: '', checkpoint: '', enroll, onEvent: e => events.push(e) });
  const bridge = await attachPolarPpgVerifier(worker, evidence);
  const send = async (v: unknown) => { const count = events.length; (worker as any).child.stdin.write(JSON.stringify(v) + '\n'); for (let i = 0; i < 100 && events.length === count; i++) await pause(); };
  const cleanup = async () => { await bridge.disconnect(); rmSync(dir, { recursive: true, force: true }); };
  return { worker, evidence, events, send, cleanup };
}
const ready = { type: 'ready', version: 1, registered: true, identityId: 'primary-user' };
const device = () => ({ type: 'device', version: 1, connected: true, streaming: true, observedAt: new Date().toISOString() });
const result = (decision: string, identityId = 'primary-user') => ({ type: 'verification', version: 1, decision, identityId: decision === 'MATCH' ? identityId : null, experimental: true, similarity: .9, quality: { ppg_samples: 1350 }, verifiedAt: new Date().toISOString(), expiresAt: new Date(Date.now()+15000).toISOString() });

test('valid decisions and expired evidence remain fail closed', async () => {
  const x = await setup();
  try {
    await x.send(ready); await x.send(device()); await x.send(result('MATCH'));
    assert.equal(x.evidence.snapshot().polarPpg.status, 'MATCH');
    await x.send(result('MISMATCH')); assert.equal(x.evidence.snapshot().polarPpg.status, 'MISMATCH');
    await x.send(result('UNCERTAIN')); assert.equal(x.evidence.snapshot().polarPpg.status, 'UNCERTAIN');
    await x.send(result('MATCH'));
    await x.send({ ...result('MATCH'), expiresAt: new Date(Date.now()-1000).toISOString() });
    assert.equal(x.evidence.snapshot().polarPpg.status, 'UNAVAILABLE');
  } finally { await x.cleanup(); }
});

test('disconnect and crashed worker clear cached match', async () => {
  const x = await setup();
  try {
    await x.send(ready); await x.send(device()); await x.send(result('MATCH'));
    await x.send({ ...device(), connected: false, streaming: false });
    assert.equal(x.evidence.snapshot().polarPpg.status, 'UNAVAILABLE');
    await x.send(device()); await x.send(result('MATCH'));
    (x.worker as any).child.kill('SIGKILL'); await pause();
    assert.equal(x.evidence.snapshot().polarPpg.status, 'UNAVAILABLE');
  } finally { await x.cleanup(); }
});

test('malformed messages and identity mismatches invalidate evidence', async () => {
  for (const invalid of [result('MATCH', 'other'), {type:'verification',version:1,decision:'MATCH'}]) {
    const x = await setup();
    try {
      await x.send(ready); await x.send(device()); await x.send(result('MATCH'));
      await x.send(invalid);
      assert.equal(x.evidence.snapshot().polarPpg.status, 'UNAVAILABLE');
    } finally { await x.cleanup(); }
  }
});

test('missing profile cannot accept a match despite live stream', async () => {
  const x = await setup();
  try {
    await x.send({ type: 'ready', version: 1, registered: false, identityId: null });
    await x.send(device()); await x.send(result('MATCH'));
    assert.equal(x.evidence.snapshot().polarPpg.status, 'UNAVAILABLE');
  } finally { await x.cleanup(); }
});

test('different voice and PPG identities never authorize', async () => {
  const x = await setup();
  try {
    const at = new Date().toISOString(), expiry = new Date(Date.now()+15000).toISOString();
    x.evidence.record({ factor:'voice-speaker', decision:'MATCH', subjectId:'other', verifiedAt:at, expiresAt:expiry, verifier:'fixture', simulated:false });
    await x.send(ready); await x.send(device()); await x.send(result('MATCH'));
    assert.equal(x.evidence.snapshot().authorized, false);
    assert.equal(x.evidence.snapshot().matchedSubject, false);
  } finally { await x.cleanup(); }
});

test('enrollment progress and profile events do not authenticate', async () => {
  const x = await setup(true);
  try {
    await x.send({ type: 'ready', version: 1, registered: false, identityId: null });
    await x.send(device());
    await x.send({ type: 'enrollment.progress', version: 1, seconds: 45, totalSeconds: 90, quality: 'Signal usable' });
    await x.send({ type: 'profile', version: 1, registered: true, identityId: 'primary-user' });
    await x.send({ type: 'enrollment.completed', version: 1, identityId: 'primary-user', approvedWindows: 9 });
    assert.equal(x.events.some(event => event.type === 'enrollment.progress'), true);
    assert.equal(x.evidence.snapshot().authorized, false);
  } finally { await x.cleanup(); }
});
