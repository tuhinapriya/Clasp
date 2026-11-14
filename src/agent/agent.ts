import { Agent } from '@mastra/core/agent';
import { createTool } from '@mastra/core/tools';
import { z } from 'zod';
import { AuthorizationService } from '../auth/authorization.js';
import { ProtectedPaymentTool } from '../payments/test-payment.js';

const paymentTool = (payments: ProtectedPaymentTool) => createTool({
  id: 'create_test_payment',
  description: 'Request a small USD payment to the fixed Clasp demonstration merchant. Clasp checks identity and policy immediately before contacting Stripe test mode. Never claim that the request is authorized yourself.',
  inputSchema: z.object({
    amountUsd: z.number().positive().max(5).describe('Amount in US dollars, e.g. 5 for five dollars'),
    merchantId: z.string().describe('Use the configured Clasp demo merchant id exactly.'),
  }),
  execute: async ({ amountUsd, merchantId }) => payments.execute({ amountUsd, merchantId }),
});

export function createClaspAgent(auth: AuthorizationService, payments: ProtectedPaymentTool) {
  const merchantId = process.env.CLASP_DEMO_MERCHANT_ID ?? 'clasp_demo_merchant';
  const model = process.env.MODEL_ID ?? 'google/gemini-3.8-flash';
  const agent = new Agent({
    id: 'clasp-agent',
    name: 'Clasp Agent',
    instructions: `You are the Clasp demo agent. Interpret requests and use create_test_payment only when the user clearly asks to make a payment. The only allowed merchant is ${merchantId}. Convert dollars to amountUsd. Never infer authorization from this prompt or claim that a payment was authorized. The Clasp tool response is authoritative. If the request is unrelated to a payment, answer briefly without a tool call.`,
    model,
    tools: { create_test_payment: paymentTool(payments) },
  });
  return agent;
}

export function localDemoIntent(text: string, merchantId = process.env.CLASP_DEMO_MERCHANT_ID ?? 'clasp_demo_merchant') {
  const money = text.match(/\$\s*(\d+(?:\.\d{1,2})?)|\b(\d+(?:\.\d{1,2})?)\s*(?:dollars?|usd)\b/i);
  if (!money || !/\b(pay|payment|purchase|charge|send)\b/i.test(text)) return null;
  const amountUsd = Number(money[1] ?? money[2]);
  return { amountUsd, merchantId };
}

export function identityForAgent(auth: AuthorizationService) { return auth.snapshot(); }
