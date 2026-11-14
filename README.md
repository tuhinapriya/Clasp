# Clasp

Clasp puts a deterministic authorization check at the agent tool execution boundary. The agent can request a payment; it cannot authorize one.

## Start locally

```bash
npm install
cp .env.example .env
# Add a Stripe test key to STRIPE_SECRET_KEY. Add GOOGLE_API_KEY to enable the Mastra LLM.
npm run dev
```

Open <http://127.0.0.1:4173>. `npm run dev` starts the dashboard, API, SSE event stream, identity service, and Mastra agent in one process. Keep `.env` local. The server binds to `127.0.0.1` only.

Without credentials, you can still exercise the allow/deny authorization flow using the clearly labeled local demo intent parser. Set `GOOGLE_API_KEY` to enable the Mastra agent with `google/gemini-3.8-flash`. Set `STRIPE_SECRET_KEY` to a Stripe **test** secret beginning `sk_test_` to create a $5 test-mode PaymentIntent. The app refuses any key that does not begin with `sk_test_`. It confirms with Stripe's `pm_card_visa` test payment method; no real funds can move. The controlled demo merchant ID defaults to `clasp_demo_merchant` and can be changed in `.env`.

To replay both scenarios against the running server:

```bash
npm run demo
```

Scenario A activates a visibly simulated session and asks for $5. Scenario B revokes it and repeats the request. When Stripe credentials are configured, only Scenario A contacts Stripe; otherwise its authorization is allowed and the Stripe operation is clearly recorded as not executed because credentials are missing.

## What is enforced

- Identity starts `LOCKED`; authenticated sessions expire after `SESSION_TTL_SECONDS` (default 120) and revocation is immediate.
- Every protected payment call checks identity, an allowlisted merchant, USD currency, and the maximum amount immediately before the Stripe adapter call. Unknown tools, invalid amounts, expired sessions, and locked sessions deny by default.
- Stripe calls require a test-mode key, use a server-generated stable idempotency key scoped to the request, and return only a redacted PaymentIntent summary to the UI.
- Agent request IDs are deduplicated for retries in the running process. Stripe's idempotency key also guards duplicate network requests for a given request ID.
- Audit events include the action, identity state, allow/deny decision, reason, outcome, and whether an external API was invoked. Events are in memory and disappear on server restart.
- Dashboard updates use Server-Sent Events. Hardware cards remain disconnected until real adapters are configured.

## Model choice

The Mastra agent uses Google's `google/gemini-3.8-flash` route when `GOOGLE_API_KEY` is set. Google's Gemma API documents native function calling, but I could not verify a Gemma model route through the current Mastra model router. Gemini is the documented compatible substitution. Without the key, Clasp uses a small local parser for the payment demo; that fallback is visibly identified and does not pretend to be an LLM.

## Hardware boundary

`src/hardware/contracts.ts` defines Vexo microphone/motion/tap events, Polar PPG samples and connection status, identity verification, and presence monitoring interfaces. No Vexo or Polar device connection is claimed, and no PPG classifier is implemented. The existing Vexo Open Kit remains untouched in `vexo-open-kit/`; use its dashboard and API docs to validate real device behavior before implementing an adapter. The Vexo kit requires a team's actual band number before any connect code is written.

## Tests

```bash
npm test
npm run check
```

Tests cover locked and expired denial, immediate revocation, tool policy limits, denial before the payment gateway, and idempotency for duplicate requests. Stripe live API calls require the team's own Stripe test credentials. The Mastra LLM requires a Google API key. Hardware validation requires the physical Vexo and Polar devices.
