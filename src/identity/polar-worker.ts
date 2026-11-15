import { spawn, type ChildProcessWithoutNullStreams } from 'node:child_process';
import type { PolarPpgIdentityVerifier, FactorVerification } from './biometrics.js';
import type { PolarPpgSample } from '../hardware/contracts.js';

export type PolarWorkerEvent = Record<string, unknown>;
type Handlers = Parameters<PolarPpgIdentityVerifier['start']>[0];
type Options = { python: string; moduleDir: string; profile: string; source: string; checkpoint: string; device?: string; enroll?: boolean; replace?: boolean; onEvent?: (event: PolarWorkerEvent) => void };

/** JSONL protocol v1. A ready worker is insufficient: a fresh real stream and profile are required. */
export class PolarWorkerVerifier implements PolarPpgIdentityVerifier {
  private child: ChildProcessWithoutNullStreams | null = null;
  private handlers: Handlers | null = null;
  private ready = false;
  private registered = false;
  private streaming = false;
  private lastDeviceAt = 0;
  private timer: ReturnType<typeof setInterval> | null = null;
  private buffer = '';
  private stopping = false;
  constructor(private readonly options: Options, private readonly now = () => Date.now()) {}

  async start(handlers: Handlers): Promise<void> {
    if (this.child) throw new Error('Polar worker already running');
    this.handlers = handlers;
    this.stopping = false;
    handlers.onStatus('unavailable', 'Polar worker starting.');
    const args = ['-u', '-m', 'clasp_ppg.worker', '--profile', this.options.profile, '--pulse-source', this.options.source, '--checkpoint', this.options.checkpoint];
    if (this.options.device) args.push('--device', this.options.device);
    if (this.options.enroll) args.push('--enroll');
    if (this.options.replace) args.push('--replace');
    const child = spawn(this.options.python, args, { cwd: this.options.moduleDir, stdio: ['pipe', 'pipe', 'pipe'] });
    this.child = child;
    child.stdout.setEncoding('utf8');
    child.stdout.on('data', (chunk: string) => {
      this.buffer += chunk;
      if (this.buffer.length > 262144) { this.fail('Polar worker protocol exceeded size limit.'); child.kill(); return; }
      const lines = this.buffer.split('\n'); this.buffer = lines.pop() ?? '';
      for (const line of lines) {
        try { this.accept(JSON.parse(line)); }
        catch { this.fail('Invalid Polar worker message.'); child.kill(); return; }
      }
    });
    child.stderr.on('data', (chunk: Buffer) => console.error(`[polar-worker] ${String(chunk).trim().slice(0, 300)}`));
    child.on('error', () => this.fail('Polar worker could not start.'));
    child.on('exit', () => {
      if (this.child !== child) return;
      this.child = null;
      if (this.timer) clearInterval(this.timer);
      this.timer = null;
      this.fail(this.stopping ? 'Polar worker stopped.' : 'Polar worker exited unexpectedly.');
      this.options.onEvent?.({ type: 'exited', reason: this.stopping ? 'stopped' : 'worker_exit' });
    });
    this.timer = setInterval(() => {
      if (this.streaming && this.now() - this.lastDeviceAt > 3500) {
        this.fail('Polar worker heartbeat expired.');
        this.ready = false;
        child.kill();
      }
    }, 500);
    this.timer.unref();
  }

  /** Process one worker event. Invalid or out-of-order input invalidates evidence. */
  accept(message: unknown): void {
    if (!message || typeof message !== 'object' || Array.isArray(message)) throw new Error('Invalid message');
    const m = message as Record<string, unknown>;
    if (m.version !== 1 || typeof m.type !== 'string') throw new Error('Invalid protocol');
    if (m.type === 'ready' || m.type === 'profile') {
      if (typeof m.registered !== 'boolean' || (m.registered ? m.identityId !== 'primary-user' : m.identityId !== null)) throw new Error('Invalid profile');
      if (m.type === 'profile' && !this.ready) throw new Error('Worker not ready');
      if (m.type === 'ready' && this.ready) throw new Error('Duplicate readiness');
      this.ready = true;
      this.registered = m.registered;
      if (!this.registered) this.fail('No registered PPG profile.');
    } else if (m.type === 'device') {
      if (!this.ready || typeof m.connected !== 'boolean' || typeof m.streaming !== 'boolean' || (m.streaming && !m.connected) || typeof m.observedAt !== 'string') throw new Error('Invalid device status');
      const age = this.now() - Date.parse(m.observedAt);
      if (!Number.isFinite(age) || age < -1000 || age > 3500) throw new Error('Stale device status');
      this.lastDeviceAt = this.now();
      this.streaming = m.connected && m.streaming;
      if (!this.streaming || !this.registered) this.fail(!this.registered ? 'No registered PPG profile.' : 'Polar stream unavailable.');
      else this.handlers?.onStatus('ready');
    } else if (m.type === 'verification') {
      const decision = m.decision;
      if (!this.ready || !['MATCH', 'MISMATCH', 'UNCERTAIN'].includes(String(decision)) || (decision === 'MATCH' ? m.identityId !== 'primary-user' : m.identityId !== null) || m.experimental !== true || typeof m.verifiedAt !== 'string' || typeof m.expiresAt !== 'string' || (m.similarity !== null && (typeof m.similarity !== 'number' || !Number.isFinite(m.similarity) || Math.abs(m.similarity) > 1.001)) || (m.quality !== null && (typeof m.quality !== 'object' || Array.isArray(m.quality)))) throw new Error('Invalid verification');
      const verified = Date.parse(m.verifiedAt), expires = Date.parse(m.expiresAt), now = this.now();
      if (!Number.isFinite(verified) || !Number.isFinite(expires) || verified > now || now - verified > 15000 || expires <= now || expires <= verified || expires - verified > 30000) throw new Error('Stale verification');
      if (!this.registered || !this.streaming || now - this.lastDeviceAt > 3500) {
        this.fail('Verification received without a fresh Polar stream.');
      } else {
        if (decision === 'MATCH' && (m.similarity === null || !m.quality)) throw new Error('Incomplete MATCH');
        const result: FactorVerification = { factor: 'polar-ppg', decision: decision as FactorVerification['decision'], subjectId: decision === 'MATCH' ? 'primary-user' : null, identityId: decision === 'MATCH' ? 'primary-user' : null, verifiedAt: m.verifiedAt, expiresAt: m.expiresAt, verifier: 'pulse-ppg-experimental', simulated: false, similarity: m.similarity as number | null };
        this.handlers?.onResult(result);
      }
    } else if (m.type === 'enrollment.progress') {
      if (!this.options.enroll || typeof m.seconds !== 'number' || typeof m.totalSeconds !== 'number') throw new Error('Invalid enrollment progress');
    } else if (m.type === 'enrollment.completed') {
      if (!this.options.enroll || m.identityId !== 'primary-user' || !this.registered) throw new Error('Invalid enrollment result');
    } else if (m.type === 'enrollment.failed') {
      if (!this.options.enroll || typeof m.reason !== 'string') throw new Error('Invalid enrollment error');
      this.fail(`Polar enrollment: ${m.reason.slice(0, 200)}`);
    } else if (m.type === 'error') {
      if (typeof m.reason !== 'string') throw new Error('Invalid error');
      this.fail(`Polar worker: ${m.reason.slice(0, 200)}`);
      this.ready = false;
      this.child?.kill();
    } else if (m.type === 'stopped') {
      this.fail('Polar worker stopped.');
      this.ready = false;
    } else throw new Error('Unknown message');
    this.options.onEvent?.(m);
  }

  private fail(reason: string) {
    this.streaming = false;
    this.handlers?.onStatus('unavailable', reason);
    this.options.onEvent?.({ type: 'unavailable', reason });
  }
  async consumePpgSample(_sample: PolarPpgSample): Promise<void> { /* Worker exclusively owns the sensor. */ }
  async stop(): Promise<void> {
    this.stopping = true;
    this.fail('Polar worker stopping.');
    const child = this.child;
    if (!child) return;
    if (child.stdin.writable) child.stdin.write('{"type":"shutdown","version":1}\n');
    await new Promise<void>((resolve) => {
      const timer = setTimeout(() => { child.kill(); resolve(); }, 3000);
      child.once('exit', () => { clearTimeout(timer); resolve(); });
    });
  }
}
