import 'dotenv/config';
import express from 'express';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { randomUUID } from 'node:crypto';
import { Mastra } from '@mastra/core';
import { AuthorizationService } from './auth/authorization.js';
import { SimulatedIdentityProvider, hardwareState } from './hardware/contracts.js';
import { ProtectedPaymentTool, requestScope } from './payments/test-payment.js';
import { createClaspAgent, localDemoIntent } from './agent/agent.js';
import { publish, recentEvents, subscribe } from './events.js';

const currentDir = path.dirname(fileURLToPath(import.meta.url));
const app = express();
const auth = new AuthorizationService(new SimulatedIdentityProvider());
const payments = new ProtectedPaymentTool(auth);
const claspAgent = createClaspAgent(auth, payments);
const mastra = new Mastra({ agents: { claspAgent } });
const requestResults = new Map<string, Promise<Record<string, unknown>>>();
const expiryTicker = setInterval(() => auth.snapshot(), 1_000);
expiryTicker.unref();

app.disable('x-powered-by');
app.use(express.json({ limit: '16kb' }));
app.use(express.static(path.resolve(currentDir, '../public')));

app.get('/api/state', (_req, res) => {
  const events = recentEvents(120);
  res.json({
    identity: auth.snapshot(),
    hardware: hardwareState,
    agent: { framework: 'Mastra', model: process.env.MODEL_ID ?? 'google/gemini-3.8-flash', mode: process.env.GOOGLE_API_KEY ? 'llm' : 'local-demo-parser' },
    stripe: { configured: Boolean(process.env.STRIPE_SECRET_KEY?.startsWith('sk_test_')), mode: 'test', merchantId: process.env.CLASP_DEMO_MERCHANT_ID ?? 'clasp_demo_merchant' },
    events,
    audit: events.filter((event) => event.type === 'audit.entry').reverse(),
  });
});

app.post('/api/identity/authenticate', async (_req, res) => {
  const snapshot = await auth.authenticate();
  res.json(snapshot);
});
app.post('/api/identity/revoke', (_req, res) => res.json(auth.revoke()));

app.get('/api/events', (req, res) => {
  res.set({ 'Content-Type': 'text/event-stream', 'Cache-Control': 'no-cache, no-transform', Connection: 'keep-alive', 'X-Accel-Buffering': 'no' });
  res.flushHeaders();
  res.write(`event: snapshot\ndata: ${JSON.stringify({ identity: auth.snapshot(), hardware: hardwareState, recent: recentEvents(50) })}\n\n`);
  const unsubscribe = subscribe((event) => res.write(`id: ${event.id}\nevent: clasp\ndata: ${JSON.stringify(event)}\n\n`));
  const heartbeat = setInterval(() => res.write(': ping\n\n'), 20_000);
  req.on('close', () => { unsubscribe(); clearInterval(heartbeat); });
});

app.post('/api/agent/message', async (req, res) => {
  const text = typeof req.body?.text === 'string' ? req.body.text.trim() : '';
  const requestId = typeof req.body?.requestId === 'string' && /^[a-zA-Z0-9_-]{8,80}$/.test(req.body.requestId) ? req.body.requestId : randomUUID();
  if (!text || text.length > 1000) return res.status(400).json({ error: 'Enter a request up to 1,000 characters.' });
  const cached = requestResults.get(requestId);
  if (cached) return res.json(await cached);

  const work = requestScope.run({ requestId }, async () => {
    publish('agent.request.received', { requestId, characterCount: text.length });
    let answer: string;
    let mode: string;
    if (process.env.GOOGLE_API_KEY) {
      mode = 'mastra-llm';
      const agent = mastra.getAgentById('clasp-agent');
      const result = await agent.generate(text);
      answer = result.text || 'The agent completed the request.';
    } else {
      mode = 'local-demo-parser';
      const paymentIntent = localDemoIntent(text);
      if (!paymentIntent) {
        answer = 'I can handle a payment request to the Clasp demo merchant. Try “Pay $5 to the demo merchant.”';
      } else {
        publish('agent.tool.proposed', { requestId, tool: 'create_test_payment', amountUsd: paymentIntent.amountUsd, merchantId: paymentIntent.merchantId, mode });
        const result = await payments.execute(paymentIntent);
        answer = result.ok
          ? `Clasp allowed the request. Stripe test PaymentIntent ${result.paymentIntent?.id} is ${result.paymentIntent?.status}.`
          : result.decision === 'DENY'
            ? `Clasp blocked the request: ${result.reason} Stripe was not called.`
            : `Clasp allowed the request, but no payment was created: ${result.reason}`;
      }
    }
    publish('agent.response', { requestId, mode, characterCount: answer.length });
    return { requestId, answer, mode };
  });
  requestResults.set(requestId, work);
  if (requestResults.size > 500) requestResults.delete(requestResults.keys().next().value!);
  try {
    res.json(await work);
  } catch (error) {
    requestResults.delete(requestId);
    const message = error instanceof Error ? error.message : 'Agent request failed.';
    publish('agent.error', { requestId, message });
    res.status(502).json({ error: message, requestId });
  }
});

const port = Number(process.env.PORT ?? 4173);
app.listen(port, '127.0.0.1', () => {
  console.log(`Clasp is running at http://127.0.0.1:${port}`);
  console.log(`Identity provider: SIMULATED development provider; Vexo and Polar are disconnected until adapters are configured.`);
  console.log(`Agent mode: ${process.env.GOOGLE_API_KEY ? `Mastra + ${process.env.MODEL_ID ?? 'google/gemini-3.8-flash'}` : 'local demo parser (set GOOGLE_API_KEY to enable the Mastra LLM)'}`);
  console.log(`Stripe mode: ${process.env.STRIPE_SECRET_KEY?.startsWith('sk_test_') ? 'test credentials configured' : 'not configured; protected requests will not reach Stripe'}`);
});
