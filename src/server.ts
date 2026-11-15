import 'dotenv/config';
import express from 'express';
import { spawn, type ChildProcessWithoutNullStreams } from 'node:child_process';
import { existsSync, readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { randomUUID } from 'node:crypto';
import { Mastra } from '@mastra/core';
import { AuthorizationService } from './auth/authorization.js';
import { SimulatedIdentityProvider, hardwareState } from './hardware/contracts.js';
import type { ConnectionStatus } from './hardware/contracts.js';
import { attachPolarPpgVerifier, BiometricEvidenceService, BiometricIdentityProvider, type FactorVerification, type MatchDecision } from './identity/biometrics.js';
import { PolarWorkerVerifier } from './identity/polar-worker.js';
import { identityView } from './identity/identity-view.js';
import { VEXO_BAND_NUMBER } from './hardware/vexo-config.js';
import { ProtectedPaymentTool, requestScope } from './payments/test-payment.js';
import { createClaspAgent } from './agent/agent.js';
import { publish, recentEvents, subscribe } from './events.js';

const currentDir = path.dirname(fileURLToPath(import.meta.url));
const app = express();
const identityMode = process.env.CLASP_IDENTITY_MODE === 'development-simulation' ? 'development-simulation' : 'biometric';
const biometrics = new BiometricEvidenceService();
const identityProvider = identityMode === 'development-simulation' ? new SimulatedIdentityProvider() : new BiometricIdentityProvider(biometrics);
const auth = new AuthorizationService(identityProvider, undefined, undefined, identityMode === 'biometric' ? () => biometrics.authorizationStatus() : undefined);
const payments = new ProtectedPaymentTool(auth);
const claspAgent = createClaspAgent(payments);
const mastra = new Mastra({ agents: { claspAgent } });
const modelId = process.env.MODEL_ID ?? 'google/gemini-3.8-flash';
const requestResults = new Map<string, Promise<Record<string, unknown>>>();
type DemoTurn = { createdAt: number; generation: number; transcript?: string; work?: Promise<Record<string, unknown>> };
const demoTurns = new Map<string, DemoTurn>();
let vexoBridge: ChildProcessWithoutNullStreams | null = null;
let vexoBridgeGeneration = 0;
let vexoBridgeReady = false;
let lastVexoHeartbeatAt = 0;
let lastMotionEventAt = 0;
let voiceCaptureActive = false;
const voiceProfilePath = path.resolve(currentDir, '../.clasp/voice-profile.json');
const projectRoot = path.resolve(currentDir, '..');
let polarWorker: PolarWorkerVerifier | null = null;
let polarDisconnect: (() => Promise<void>) | null = null;
let polarProfileRegistered = false;
let polarLastVerification: Record<string, unknown> | null = null;
let polarDevice: Record<string, unknown> | null = null;
let lastAuthenticatedAt: string | null = null;
const polarProfilePath = process.env.CLASP_PPG_PROFILE ?? path.join(projectRoot, '.clasp/ppg-profile.json');

function polarProfileStatus() {
  try {
    const p = JSON.parse(readFileSync(polarProfilePath, 'utf8'));
    const vectors = Array.isArray(p.embeddings) ? p.embeddings : (Array.isArray(p.centroid) ? [p.centroid] : []);
    const valid = (([1, 2].includes(p.schemaVersion) && p.model === 'papagei-p') || (p.schemaVersion === 3 && p.model === 'pulse-ppg')) && p.identityId === 'primary-user' && typeof p.sourceSessionId === 'string' && vectors.length >= 1 && vectors.every((v: unknown) => Array.isArray(v) && v.length === 512 && v.every((n: unknown) => typeof n === 'number' && Number.isFinite(n)) && v.some((n: number) => n !== 0));
    return { registered: valid, existing: true, identityId: valid ? 'primary-user' : null, reEnrollmentRequired: valid && (p.model !== 'pulse-ppg' || vectors.length < 9), templateCount: vectors.length };
  } catch { return { registered: false, existing: existsSync(polarProfilePath), identityId: null, reEnrollmentRequired: false, templateCount: 0 }; }
}

function voiceProfileMetadata() {
  try {
    const profile = JSON.parse(readFileSync(voiceProfilePath, 'utf8'));
    if (profile.model !== 'speechbrain/spkrec-ecapa-voxceleb' || typeof profile.identityId !== 'string') throw new Error('Invalid local voice profile.');
    return { registered: true, identityId: profile.identityId, enrolledAt: profile.enrolledAt ?? null, reEnrolledAt: profile.reEnrolledAt ?? null, model: profile.model, reEnrollmentRequired: profile.identityId !== 'primary-user' };
  } catch {
    return { registered: existsSync(voiceProfilePath), identityId: null, enrolledAt: null, reEnrolledAt: null, model: 'speechbrain/spkrec-ecapa-voxceleb', reEnrollmentRequired: existsSync(voiceProfilePath) };
  }
}
const biometricSignature = (snapshot: ReturnType<BiometricEvidenceService['snapshot']>) => JSON.stringify({ voice: snapshot.voiceSpeaker.status, polar: snapshot.polarPpg.status, authorized: snapshot.authorized });
let lastBiometricSignature = biometricSignature(biometrics.snapshot());

if (identityMode === 'biometric') {
  biometrics.onChange((snapshot, reason) => {
    lastBiometricSignature = biometricSignature(snapshot);
    const current = auth.snapshot();
    if (snapshot.authorized && current.state !== 'AUTHENTICATED') void auth.authenticate();
    else if (!snapshot.authorized && current.state === 'AUTHENTICATED' && !current.simulated) auth.revoke(reason);
  });
}

subscribe((event) => {
  if (event.type === 'identity.authenticated' && event.data.simulated !== true) lastAuthenticatedAt = event.at;
  if (event.type === 'hardware.vexo.connection' && event.data.status !== 'connected' && identityMode === 'biometric') {
    biometrics.invalidate('voice-speaker', `Vexo audio source disconnected: ${String(event.data.detail ?? 'connection ended')}`);
  }
});
const expiryTicker = setInterval(() => {
  auth.snapshot();
  const factors = biometrics.snapshot();
  const signature = biometricSignature(factors);
  if (signature !== lastBiometricSignature) {
    lastBiometricSignature = signature;
    publish('identity.biometric_factors', {
      voiceSpeaker: factors.voiceSpeaker.status,
      polarPpg: factors.polarPpg.status,
      matchedSubject: factors.matchedSubject,
      authorized: factors.authorized,
      reason: 'A biometric verifier result expired or became stale.',
    });
  }
  if (hardwareState.vexo.status === 'connected' && Date.now() - lastVexoHeartbeatAt > 5_000) stopVexoBridge('Vexo connection heartbeat expired.');
}, 1_000);
expiryTicker.unref();

app.disable('x-powered-by');
app.use(express.json({ limit: '16kb' }));
app.use(express.static(path.resolve(currentDir, '../public')));

const expectedBandName = `Vexo-HACK-${String(VEXO_BAND_NUMBER).padStart(2, '0')}`;

function setVexoConnection(status: ConnectionStatus, detail: string, bandNumber: number | null = null, name: string | null = null) {
  hardwareState.vexo.status = status;
  hardwareState.vexo.detail = detail;
  hardwareState.vexo.bandNumber = status === 'connected' ? bandNumber : null;
  hardwareState.vexo.name = status === 'connected' ? name : null;
  if (status !== 'connected') { hardwareState.vexo.microphoneOn = false; voiceCaptureActive = false; }
  publish('hardware.vexo.connection', { status, detail, bandNumber: hardwareState.vexo.bandNumber, name: hardwareState.vexo.name });
}

app.get('/api/state', (_req, res) => {
  const events = recentEvents(160);
  res.json({
    identity: auth.snapshot(),
    identityMode,
    biometrics: biometrics.snapshot(),
    voiceProfile: voiceProfileMetadata(),
    polarVerifier: { running: polarWorker !== null, profileRegistered: polarProfileStatus().registered, profile: polarProfileStatus(), device: polarDevice, latestVerification: polarLastVerification },
    identityView: identityView(auth.snapshot(), biometrics.authorizationStatus(), identityMode === 'development-simulation', lastAuthenticatedAt),
    hardware: { ...hardwareState, vexo: { ...hardwareState.vexo, expectedBandNumber: VEXO_BAND_NUMBER } },
    agent: { framework: 'Mastra', provider: 'Google', model: modelId, configured: Boolean(process.env.GOOGLE_API_KEY), mode: process.env.GOOGLE_API_KEY ? 'real-llm-configured' : 'unavailable' },
    stripe: { configured: Boolean(process.env.STRIPE_SECRET_KEY?.startsWith('sk_test_')), mode: 'test', merchantId: process.env.CLASP_DEMO_MERCHANT_ID ?? 'clasp_demo_merchant' },
    events,
    audit: events.filter((event) => event.type === 'audit.entry').reverse(),
  });
});

app.post('/api/identity/authenticate', async (_req, res) => {
  if (identityMode !== 'development-simulation') return res.status(403).json({ error: 'Manual identity simulation is disabled. Both biometric verifier results are required.' });
  const snapshot = await auth.authenticate();
  res.json(snapshot);
});
app.post('/api/identity/revoke', (_req, res) => res.json(auth.revoke()));

async function startPolarWorker(req: express.Request, res: express.Response, enroll: boolean) {
  if (identityMode !== 'biometric') return res.status(403).json({ error: 'Polar biometric worker is disabled in development simulation mode.' });
  if (polarWorker) return res.status(409).json({ error: 'Polar worker is already running.' });
  const replace = req.body?.replace === true;
  if (enroll && polarProfileStatus().existing && !replace) return res.status(409).json({ error: 'A PPG profile exists. Explicit re-enrollment is required.' });
  if (enroll) biometrics.invalidate('polar-ppg', 'PPG enrollment started; fresh verification is required.');
  const moduleDir = path.join(projectRoot, 'clasp-ppg');
  const worker = new PolarWorkerVerifier({
    python: process.env.CLASP_PPG_PYTHON ?? path.join(moduleDir, '.venv/bin/python'),
    moduleDir, profile: polarProfilePath, enroll, replace,
    source: process.env.CLASP_PULSEPPG_SOURCE ?? path.join(projectRoot, '.clasp/pulseppg-source'),
    checkpoint: process.env.CLASP_PULSEPPG_CHECKPOINT ?? path.join(projectRoot, '.clasp/pulseppg-checkpoint.pkl'),
    onEvent: (event) => {
      if (event.type === 'profile' || event.type === 'ready') polarProfileRegistered = event.registered === true;
      if (event.type === 'verification') polarLastVerification = event;
      if (event.type === 'unavailable' || event.type === 'stopped') polarLastVerification = null;
      if (event.type === 'device') {
        polarDevice = event;
        hardwareState.polar.status = event.connected === true && event.streaming === true ? 'connected' : 'disconnected';
        hardwareState.polar.detail = event.streaming === true ? 'Receiving Polar PPG and accelerometer packets.' : String(event.reason ?? 'Polar stream unavailable.');
      }
      if (event.type === 'unavailable' || event.type === 'stopped') {
        polarDevice = null;
        hardwareState.polar.status = 'disconnected';
        hardwareState.polar.detail = String(event.reason ?? 'Polar worker stopped.');
      }
      publish('hardware.polar.worker', event);
      if (event.type === 'exited' && polarWorker === worker) { polarWorker = null; polarDisconnect = null; }
    },
  });
  polarWorker = worker;
  try {
    const bridge = await attachPolarPpgVerifier(worker, biometrics);
    polarDisconnect = bridge.disconnect;
    return res.status(202).json({ starting: true });
  } catch {
    polarWorker = null;
    biometrics.setUnavailable('polar-ppg', 'Could not start Polar worker.');
    return res.status(503).json({ error: 'Could not start Polar worker.' });
  }
}

app.post('/api/hardware/polar/start', (req, res) => startPolarWorker(req, res, false));
app.post('/api/hardware/polar/register', (req, res) => startPolarWorker(req, res, true));

app.post('/api/hardware/polar/stop', async (_req, res) => {
  const worker = polarWorker;
  if (!worker) return res.json({ stopped: true });
  await (polarDisconnect ? polarDisconnect() : worker.stop());
  if (polarWorker === worker) { polarWorker = null; polarDisconnect = null; }
  res.json({ stopped: true });
});

function stopVexoBridge(reason: string) {
  const child = vexoBridge;
  if (!child) return;
  vexoBridgeReady = false;
  lastVexoHeartbeatAt = 0;
  if (child.stdin.writable) child.stdin.write(`${JSON.stringify({ command: 'disconnect' })}\n`);
  const killTimer = setTimeout(() => { if (vexoBridge === child) child.kill(); }, 3_000);
  killTimer.unref();
  if (hardwareState.vexo.status !== 'disconnected') setVexoConnection('disconnected', reason);
}

function handleVexoMessage(generation: number, message: Record<string, any>) {
  if (generation !== vexoBridgeGeneration) return;
  switch (message.type) {
    case 'connected': {
      if (message.bandNumber !== VEXO_BAND_NUMBER || message.name !== expectedBandName || message.transport !== 'ble') {
        publish('hardware.vexo.error', { reason: `SDK identified an unexpected device; expected ${expectedBandName}.` });
        stopVexoBridge('Unexpected Vexo identity; connection closed.');
        return;
      }
      vexoBridgeReady = true;
      lastVexoHeartbeatAt = Date.now();
      setVexoConnection('connected', `Connected by actual Vexo Open Kit BLE SDK (${message.firmware ?? 'firmware unknown'}).`, message.bandNumber, message.name);
      publish('hardware.vexo.info', { bandNumber: message.bandNumber, name: message.name, transport: message.transport, firmware: message.firmware, capabilities: message.capabilities, source: 'verified Vexo Open Kit SDK connection' });
      publish('voice.profile', voiceProfileMetadata());
      return;
    }
    case 'voice.profile':
      publish('voice.profile', voiceProfileMetadata());
      return;
    case 'voice.status':
      publish('voice.status', { status: message.status, model: message.model, turnId: message.turnId });
      return;
    case 'voice.capture.started':
      publish('voice.capture.started', { kind: message.kind, turnId: message.turnId, durationSeconds: message.durationSeconds, source: message.source });
      return;
    case 'voice.capture.completed':
      publish('voice.capture.completed', { kind: message.kind, turnId: message.turnId, source: message.source });
      return;
    case 'voice.registration.completed':
      voiceCaptureActive = false;
      publish('voice.registration.completed', { turnId: message.turnId, profile: voiceProfileMetadata(), source: message.source, capturedSeconds: message.profile?.capturedSeconds, speechSeconds: message.profile?.speechSeconds });
      return;
    case 'voice.registration.failed':
      voiceCaptureActive = false;
      publish('voice.registration.failed', { turnId: message.turnId, reason: typeof message.reason === 'string' ? message.reason.slice(0, 240) : 'Voice registration failed.' });
      return;
    case 'voice.capture.failed':
      voiceCaptureActive = false;
      publish('voice.capture.failed', { turnId: message.turnId, reason: typeof message.reason === 'string' ? message.reason.slice(0, 240) : 'Vexo voice capture failed.' });
      return;
    case 'voice.test.completed':
      voiceCaptureActive = false;
      publish('voice.test.completed', { decision: message.decision, similarity: message.similarity, reason: message.reason, audioUsable: message.audioUsable, speechSeconds: message.speechSeconds, capturedSeconds: message.capturedSeconds });
      return;
    case 'voice.validation.completed': {
      voiceCaptureActive = false;
      const decision: MatchDecision = ['MATCH', 'MISMATCH', 'UNCERTAIN'].includes(message.decision) && (message.decision !== 'MATCH' || (message.identityId === 'primary-user' && voiceProfileMetadata().identityId === 'primary-user')) ? message.decision as MatchDecision : 'UNCERTAIN';
      const result: FactorVerification = {
        factor: 'voice-speaker',
        decision,
        subjectId: decision === 'MATCH' && typeof message.identityId === 'string' ? message.identityId : null,
        identityId: decision === 'MATCH' ? 'primary-user' : null,
        verifiedAt: typeof message.verifiedAt === 'string' ? message.verifiedAt : new Date().toISOString(),
        expiresAt: typeof message.expiresAt === 'string' ? message.expiresAt : new Date(0).toISOString(),
        verifier: typeof message.verifier === 'string' ? message.verifier : 'speechbrain/spkrec-ecapa-voxceleb',
        simulated: false,
        similarity: Number.isFinite(message.similarity) ? message.similarity : null,
        audioSource: message.audioSource && typeof message.audioSource === 'object' ? message.audioSource : {},
      };
      const sourceIsCurrentBand = hardwareState.vexo.status === 'connected'
        && result.audioSource?.bandNumber === hardwareState.vexo.bandNumber
        && result.audioSource?.device === hardwareState.vexo.name;
      const accepted = message.simulated === false && sourceIsCurrentBand && biometrics.record(result);
      if (!sourceIsCurrentBand) biometrics.invalidate('voice-speaker', 'Vexo disconnected or changed before speaker verification completed.');
      publish('identity.voice_verification', {
        turnId: message.turnId,
        decision: accepted ? decision : 'UNCERTAIN',
        similarity: result.similarity,
        identityId: result.identityId,
        verifiedAt: result.verifiedAt,
        expiresAt: result.expiresAt,
        audioSource: result.audioSource,
        model: result.verifier,
        reason: typeof message.reason === 'string' ? message.reason.slice(0, 240) : (accepted ? 'Speaker verification completed.' : 'Speaker verification result was rejected by Clasp.'),
        accepted,
      });
      return;
    }
    case 'voice.transcription': {
      const turn = typeof message.turnId === 'string' ? demoTurns.get(message.turnId) : undefined;
      if (turn && Date.now() - turn.createdAt < 120_000 && hardwareState.vexo.status === 'connected' && message.source?.device === hardwareState.vexo.name && message.source?.bandNumber === hardwareState.vexo.bandNumber && typeof message.text === 'string' && message.text.trim().length > 0) turn.transcript = message.text.trim().slice(0, 1000);
      publish('voice.transcription', { turnId: message.turnId, text: typeof message.text === 'string' ? message.text.slice(0, 1000) : '', speakerDecision: message.speakerDecision, reason: typeof message.reason === 'string' ? message.reason.slice(0, 240) : null, source: message.source, local: true });
      return;
    }
    case 'voice.error':
      publish('voice.error', { reason: typeof message.reason === 'string' ? message.reason.slice(0, 240) : 'Local voice verifier error.' });
      return;
    case 'heartbeat':
      if (vexoBridgeReady && (message.bandNumber !== VEXO_BAND_NUMBER || message.name !== expectedBandName)) stopVexoBridge('Vexo bridge identity became uncertain.');
      else if (vexoBridgeReady) lastVexoHeartbeatAt = Date.now();
      return;
    case 'motion': {
      if (!vexoBridgeReady || hardwareState.vexo.status !== 'connected') return;
      const now = Date.now();
      if (now - lastMotionEventAt < 200) return;
      lastMotionEventAt = now;
      const sample = message.sample ?? {};
      const clean: Record<string, number | null> = {};
      for (const field of ['ax', 'ay', 'az', 'gx', 'gy', 'gz']) {
        const value = sample[field];
        clean[field] = value == null ? null : Number.isFinite(value) ? Math.round(value * 1000) / 1000 : null;
      }
      publish('hardware.vexo.motion', { sampleTime: Number.isFinite(message.time) ? message.time : null, motion: clean, source: 'real Vexo Open Kit motion event' });
      return;
    }
    case 'gesture': {
      if (!vexoBridgeReady || hardwareState.vexo.status !== 'connected') return;
      const allowed = new Set(['tap', 'double-tap', 'free-fall', 'tilt', 'orientation', 'motion-start', 'motion-stop']);
      if (!allowed.has(message.gesture)) return;
      publish('hardware.vexo.gesture', { gesture: message.gesture, sampleTime: Number.isFinite(message.time) ? message.time : null, source: 'real Vexo Open Kit event' });
      return;
    }
    case 'link':
      if (vexoBridgeReady && hardwareState.vexo.status === 'connected') publish('hardware.vexo.link', { rssi: Number.isFinite(message.rssi) ? message.rssi : null, phy: typeof message.phy === 'string' ? message.phy : null, sampleTime: Number.isFinite(message.time) ? message.time : null });
      return;
    case 'microphone':
      if (vexoBridgeReady) {
        hardwareState.vexo.microphoneOn = message.active === true;
        publish('hardware.vexo.microphone', { active: hardwareState.vexo.microphoneOn, source: 'Vexo Open Kit microphone state; not speaker verification' });
      }
      return;
    case 'audio-summary':
      if (vexoBridgeReady && hardwareState.vexo.microphoneOn && Number.isInteger(message.packets) && message.packets > 0 && Number.isInteger(message.lastSequence)) {
        publish('hardware.vexo.audio', { packets: message.packets, lastSequence: message.lastSequence, sampleTime: Number.isFinite(message.time) ? message.time : null, source: 'real Vexo Open Kit audio telemetry; not speaker verification' });
      }
      return;
    case 'disconnected':
      vexoBridgeReady = false;
      setVexoConnection('disconnected', typeof message.reason === 'string' ? `Vexo disconnected (${message.reason}).` : 'Vexo disconnected.');
      return;
    case 'error':
      vexoBridgeReady = false;
      publish('hardware.vexo.error', { reason: typeof message.message === 'string' ? message.message.slice(0, 240) : 'Vexo bridge failed.' });
      if (hardwareState.vexo.status !== 'disconnected') setVexoConnection('disconnected', 'Vexo bridge failed; presence is uncertain.');
      vexoBridge?.kill();
      return;
    default:
      return;
  }
}

app.post('/api/hardware/vexo/connect', (_req, res) => {
  if (vexoBridge) return res.status(409).json({ error: 'A Vexo connection attempt is already running.' });
  const projectRoot = path.resolve(currentDir, '..');
  const bridgeScript = path.join(projectRoot, 'scripts', 'vexo-bridge.py');
  const kitRoot = path.join(projectRoot, 'vexo-open-kit', 'python');
  if (!existsSync(bridgeScript) || !existsSync(kitRoot)) return res.status(503).json({ error: 'Local Vexo Open Kit files are missing. Clone the kit into ./vexo-open-kit first.' });
  const venvPython = path.join(projectRoot, '.venv', 'bin', 'python');
  const python = process.env.VEXO_PYTHON ?? (existsSync(venvPython) ? venvPython : 'python3');
  const generation = ++vexoBridgeGeneration;
  const child = spawn(python, [bridgeScript, String(VEXO_BAND_NUMBER)], { cwd: projectRoot, stdio: ['pipe', 'pipe', 'pipe'] });
  vexoBridge = child;
  vexoBridgeReady = false;
  lastVexoHeartbeatAt = 0;
  setVexoConnection('unknown', `Connecting to ${expectedBandName} through the actual Vexo Open Kit BLE SDK.`);
  let output = '';
  child.stdout.setEncoding('utf8');
  child.stdout.on('data', (chunk: string) => {
    output += chunk;
    const lines = output.split('\n');
    output = lines.pop() ?? '';
    for (const line of lines) {
      if (!line.trim()) continue;
      try { handleVexoMessage(generation, JSON.parse(line)); }
      catch { publish('hardware.vexo.error', { reason: 'The Vexo bridge returned an invalid SDK event.' }); stopVexoBridge('Invalid Vexo SDK event.'); }
    }
  });
  child.stderr.setEncoding('utf8');
  child.stderr.on('data', (chunk: string) => console.error(`[vexo-open-kit] ${String(chunk).trim().slice(0, 300)}`));
  child.on('error', () => {
    if (generation !== vexoBridgeGeneration) return;
    vexoBridge = null;
    vexoBridgeReady = false;
    setVexoConnection('disconnected', `Could not start ${python}. Run npm run setup:vexo.`);
  });
  child.on('exit', (code, signal) => {
    if (generation !== vexoBridgeGeneration) return;
    vexoBridge = null;
    vexoBridgeReady = false;
    if (hardwareState.vexo.status !== 'disconnected') setVexoConnection('disconnected', `Vexo bridge stopped (${signal ?? code ?? 'unknown'}).`);
    if (output.trim()) {
      try { handleVexoMessage(generation, JSON.parse(output)); }
      catch { /* a truncated final line is not a sensor event */ }
    }
  });
  res.status(202).json({ connecting: true, bandName: expectedBandName, message: `Connecting only to ${expectedBandName}. Double-tap the band if it is not discoverable.` });
});

app.post('/api/hardware/vexo/disconnect', (_req, res) => {
  if (!vexoBridge) {
    if (hardwareState.vexo.status !== 'disconnected') setVexoConnection('disconnected', 'Disconnected from the Clasp dashboard.');
    return res.json({ connected: false });
  }
  stopVexoBridge('Disconnected from the Clasp dashboard.');
  res.status(202).json({ disconnected: true });
});

app.post('/api/demo/listen', (req, res) => {
  if (identityMode !== 'biometric') return res.status(403).json({ error: 'Real biometric mode is required for this demo.' });
  if (!vexoBridgeReady || hardwareState.vexo.status !== 'connected' || !vexoBridge?.stdin.writable) return res.status(409).json({ error: 'Connect the Vexo band before listening.' });
  if (voiceCaptureActive) return res.status(409).json({ error: 'Another voice capture is active.' });
  for (const [id, turn] of demoTurns) if (Date.now() - turn.createdAt > 120_000) demoTurns.delete(id);
  biometrics.invalidate('voice-speaker', 'New spoken request requires fresh voice verification.');
  const turnId = randomUUID();
  demoTurns.set(turnId, { createdAt: Date.now(), generation: vexoBridgeGeneration });
  voiceCaptureActive = true;
  vexoBridge.stdin.write(`${JSON.stringify({ command: 'capture-voice', kind: 'agent-turn', turnId })}\n`);
  publish('demo.listening', { turnId, simulated: false, source: 'Vexo microphone' });
  res.status(202).json({ turnId, simulated: false });
});

app.post('/api/demo/run', async (req, res) => {
  const turnId = typeof req.body?.turnId === 'string' ? req.body.turnId : '';
  const turn = demoTurns.get(turnId);
  if (!turn || Date.now() - turn.createdAt >= 120_000 || !turn.transcript || turn.generation !== vexoBridgeGeneration || hardwareState.vexo.status !== 'connected') return res.status(409).json({ error: 'No fresh completed Vexo recording is available. Start listening again.' });
  if (!process.env.GOOGLE_API_KEY) return res.status(503).json({ error: 'Google model is not configured.' });
  if (!turn.work) {
    const transcript = turn.transcript;
    publish('demo.reasoning', { requestId: turnId, simulated: false });
    turn.work = requestScope.run({ requestId: turnId, demo: true, getEvidence: () => biometrics.snapshot(), isStillValid: () => vexoBridgeReady && hardwareState.vexo.status === 'connected' && turn.generation === vexoBridgeGeneration }, async () => {
      const result = await mastra.getAgentById('clasp-agent').generate(transcript);
      return { requestId: turnId, answer: result.text || 'The model returned no text.', usedRealLlm: true, model: modelId, simulatedBiometricDecisions: false };
    });
  }
  try { res.json(await turn.work); }
  catch { res.status(502).json({ error: 'The Google model request failed.' }); }
});

app.post('/api/voice/register', (req, res) => {
  if (!vexoBridgeReady || hardwareState.vexo.status !== 'connected' || !vexoBridge?.stdin.writable) return res.status(409).json({ error: 'Connect Vexo-HACK-78 in Clasp before registering a voice.' });
  if (voiceCaptureActive) return res.status(409).json({ error: 'Another Vexo voice capture is already active.' });
  const existing = voiceProfileMetadata();
  const replace = req.body?.replace === true;
  if (existing.registered && !replace) return res.status(409).json({ requiresConfirmation: true, profile: existing, error: 'A voice profile already exists. Confirm explicit re-enrollment to replace it.' });
  biometrics.invalidate('voice-speaker', 'Voice enrollment started; fresh speaker verification is required.');
  voiceCaptureActive = true;
  const turnId = randomUUID();
  vexoBridge.stdin.write(`${JSON.stringify({ command: 'capture-voice', kind: 'registration', turnId, replace })}\n`);
  publish('voice.registration.requested', { turnId, replacing: existing.registered && replace, identityId: existing.registered ? existing.identityId : null });
  res.status(202).json({ started: true, turnId, replacing: existing.registered && replace, message: 'Vexo microphone capture started; speak naturally for about ten seconds.' });
});

app.post('/api/voice/test', (_req, res) => {
  if (!vexoBridgeReady || hardwareState.vexo.status !== 'connected' || !vexoBridge?.stdin.writable) return res.status(409).json({ error: 'Connect Vexo before testing.' });
  if (!voiceProfileMetadata().registered) return res.status(409).json({ error: 'Register a voice first.' });
  if (voiceCaptureActive) return res.status(409).json({ error: 'Another capture is active.' });
  voiceCaptureActive = true;
  const turnId = randomUUID();
  vexoBridge.stdin.write(`${JSON.stringify({ command: 'capture-voice', kind: 'test', turnId })}\n`);
  res.status(202).json({ started: true, turnId });
});

app.post('/api/voice/stop', (_req, res) => {
  if (!voiceCaptureActive || !vexoBridge?.stdin.writable) return res.status(409).json({ error: 'No voice capture is active.' });
  vexoBridge.stdin.write(`${JSON.stringify({ command: 'stop-voice' })}\n`);
  res.status(202).json({ stopping: true });
});

app.post('/api/voice/talk', (req, res) => {
  if (!vexoBridgeReady || hardwareState.vexo.status !== 'connected' || !vexoBridge?.stdin.writable) return res.status(409).json({ error: 'Connect Vexo-HACK-78 in Clasp before speaking to the agent.' });
  if (voiceCaptureActive) return res.status(409).json({ error: 'Another Vexo voice capture is already active.' });
  const turnId = typeof req.body?.turnId === 'string' && /^[a-zA-Z0-9_-]{8,80}$/.test(req.body.turnId) ? req.body.turnId : randomUUID();
  voiceCaptureActive = true;
  vexoBridge.stdin.write(`${JSON.stringify({ command: 'capture-voice', kind: 'agent-turn', turnId })}\n`);
  publish('voice.agent_turn.requested', { turnId, identityId: voiceProfileMetadata().identityId, source: 'Vexo Open Kit microphone' });
  res.status(202).json({ started: true, turnId, message: 'Speak your request naturally; audio is processed locally on this Mac.' });
});

app.post('/api/hardware/vexo/microphone', (req, res) => {
  if (!vexoBridgeReady || hardwareState.vexo.status !== 'connected') return res.status(409).json({ error: 'Vexo is not connected.' });
  if (typeof req.body?.active !== 'boolean') return res.status(400).json({ error: 'Microphone active must be true or false.' });
  const command = req.body.active ? 'microphone-on' : 'microphone-off';
  if (!vexoBridge?.stdin.writable) return res.status(409).json({ error: 'Vexo bridge is unavailable.' });
  vexoBridge.stdin.write(`${JSON.stringify({ command })}\n`);
  res.json({ accepted: true, active: req.body.active, rawAudioForwarded: false });
});

app.get('/api/events', (req, res) => {
  res.set({ 'Content-Type': 'text/event-stream', 'Cache-Control': 'no-cache, no-transform', Connection: 'keep-alive', 'X-Accel-Buffering': 'no' });
  res.flushHeaders();
  res.write(`event: snapshot\ndata: ${JSON.stringify({ identity: auth.snapshot(), identityMode, biometrics: biometrics.snapshot(), voiceProfile: voiceProfileMetadata(), hardware: hardwareState, recent: recentEvents(80) })}\n\n`);
  const unsubscribe = subscribe((event) => res.write(`id: ${event.id}\nevent: clasp\ndata: ${JSON.stringify(event)}\n\n`));
  const heartbeat = setInterval(() => res.write(': ping\n\n'), 20_000);
  req.on('close', () => { unsubscribe(); clearInterval(heartbeat); });
});

app.post('/api/agent/message', async (req, res) => {
  const text = typeof req.body?.text === 'string' ? req.body.text.trim() : '';
  const requestId = typeof req.body?.requestId === 'string' && /^[a-zA-Z0-9_-]{8,80}$/.test(req.body.requestId) ? req.body.requestId : randomUUID();
  if (!text || text.length > 1000) return res.status(400).json({ error: 'Enter a request up to 1,000 characters.' });
  if (!process.env.GOOGLE_API_KEY) {
    publish('agent.request.rejected', { requestId, provider: 'Google', model: modelId, usedRealLlm: false, reason: 'GOOGLE_API_KEY is missing.' });
    return res.status(503).json({ error: 'Google model is not configured. Set GOOGLE_API_KEY in .env; no local parser fallback is used.', model: modelId, usedRealLlm: false });
  }
  const cached = requestResults.get(requestId);
  if (cached) return res.json(await cached);

  const work = requestScope.run({ requestId }, async () => {
    publish('agent.request.started', { requestId, provider: 'Google', model: modelId, usedRealLlm: true, textLength: text.length });
    const agent = mastra.getAgentById('clasp-agent');
    const result = await agent.generate(text);
    const answer = result.text || 'The real model completed the request without a text response.';
    publish('agent.response', { requestId, provider: 'Google', model: modelId, usedRealLlm: true, characterCount: answer.length });
    return { requestId, answer, mode: 'real-llm', usedRealLlm: true, model: modelId };
  });
  requestResults.set(requestId, work);
  if (requestResults.size > 500) requestResults.delete(requestResults.keys().next().value!);
  try {
    res.json(await work);
  } catch {
    requestResults.delete(requestId);
    publish('agent.error', { requestId, provider: 'Google', model: modelId, usedRealLlm: true, reason: 'The configured model request failed.' });
    res.status(502).json({ error: 'The Google model request failed. Check the model ID and server-side Google API key.', requestId, model: modelId, usedRealLlm: true });
  }
});

const port = Number(process.env.PORT ?? 4173);
app.listen(port, '127.0.0.1', () => {
  console.log(`Clasp is running at http://127.0.0.1:${port}`);
  console.log(`Identity mode: ${identityMode}; both fresh real biometric matches are required outside explicit development simulation.`);
  console.log(`Vexo: expects ${expectedBandName}; live BLE data comes from the local Vexo Open Kit Python SDK bridge.`);
  console.log(`Agent: ${process.env.GOOGLE_API_KEY ? `Google ${modelId} configured` : `UNAVAILABLE (set GOOGLE_API_KEY to enable ${modelId})`}`);
  console.log(`Stripe: ${process.env.STRIPE_SECRET_KEY?.startsWith('sk_test_') ? 'test key configured' : 'UNAVAILABLE (set STRIPE_SECRET_KEY to an sk_test_ key)'}`);
});
