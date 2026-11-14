# Clasp — Project Context

## One-line pitch
**Clasp binds an AI agent's sensitive permissions to the continued presence and verified identity of its human owner.**

**Tagline:** Your agent has your permissions. Does it still have you?

## Why this exists
Agents can hold credentials for email, calendars, payments, and other tools. An agent having access to a tool does not mean every invocation is authorized by the human owner. Clasp adds a deterministic authorization layer at the **tool-execution boundary**, using a wearable as one source of identity and presence evidence.

This is **not** a new payment wallet, a generic voice assistant, or a claim of production-grade biometric authentication.

## Hackathon goal
Build a convincing, live, end-to-end demonstration:
1. Owner wears Vexo and Polar Verity Sense and explicitly authenticates.
2. Agent receives a spoken request, selects a sensitive tool, and executes it **only when Clasp authorizes**.
3. Remove/disconnect the wearable or invalidate the identity session.
4. Agent still selects the same tool, but Clasp **blocks execution before any external side effect**.
5. Show an audit trail with requested tool, identity/presence status, decision, reason, and whether the external API was invoked.

**North star:** The agent can request an action; it cannot grant itself permission to execute it.

## Available hardware and services
- **Vexo band:** microphone, motion/accelerometer, possibly tap/gesture and LED through `vexo-open-kit`. Validate each capability on the actual hardware; don't assume an API works because it is described elsewhere.
- **Polar Verity Sense:** optical PPG sensor over BLE. Explore raw waveform streaming, quality, and identity matching. Raw PPG biometric recognition is **experimental**, not payment-grade. Presence/removal detection must be empirically tested; do not infer presence from heart-rate values alone.
- **MacBook:** runs local orchestration, BLE clients, backend, dashboard.
- **Deepgram:** live speech-to-text.
- **Mastra:** AI agent and tool orchestration.
- **Gemma / Google credits:** preferred LLM for agent intent/tool selection **if integration is straightforward**; otherwise use the fastest working supported model. Do not spend the day on model integration.
- **Stripe:** test-mode tool with visible, verifiable execution; no live money movement.
- **ElevenLabs:** optional spoken confirmations, only after the core flow works.
- **MiniMax:** optional alternative LLM/voice provider; not necessary for MVP.

## Repository reference
- Vexo Open Kit: https://github.com/VexoAI-Org/vexo-open-kit
- Inspect the repository README and examples before implementing BLE/audio/gesture logic. Confirm actual command paths and SDK interfaces against the checked-out version.

## Core architecture
```text
Vexo mic / gesture ──> Deepgram ──> Mastra agent (LLM)
                                          │
                                    Proposed tool call
                                          │
                                          ▼
                                  Clasp authorization
                                          ▲
                           Identity/presence session state
                           ▲                      ▲
                     Vexo signals            Polar PPG/BLE
                                          │
                          ALLOW ───────────┴────────── DENY
                            │                           │
                     Protected tool                 Audit only
                     (Stripe test)              (no API call)
                            │
                         Audit event
```

## Security model and invariants
1. **Fail closed.** Missing, stale, low-quality, or disconnected identity signals cause sensitive tools to be denied.
2. **Enforce at execution time.** A login check, UI lock, or LLM prompt is insufficient. Every sensitive tool invocation must pass the backend policy check immediately before execution.
3. **No agent bypass.** Agent code does not hold direct unrestricted credentials for protected tools. Wrap/mediate all sensitive tools behind the authorization gateway.
4. **Separate intent from authorization.** The LLM may parse a command or propose a tool call; it never sets `authorized=true` or decides biometric validity.
5. **Bind command to speaker.** A nearby person must not be able to issue a payment merely because the owner is wearing the band. Prototype speaker verification and/or explicit user confirmation; deny high-risk actions when uncertain.
6. **Presence is not consent.** A valid wearable session alone does not authorize arbitrary spending. Use explicit user intent, transaction details, and policy (e.g., test-mode amount limits).
7. **Short-lived session.** Identity verification creates a short-lived authorization session; invalidate on disconnection, removal evidence, identity mismatch, expiry, or manual lock.
8. **Audit without secrets.** Log tool name, redacted arguments, decision, reason, timestamps, and whether side effects occurred; never log API keys or raw biometric samples to the dashboard.
9. **Prototype caveat.** PPG identification is research-grade; do not claim secure continuous identity proof, anti-spoofing, or instant removal detection without validation.

## Suggested state machine
- `LOCKED`: default; protected tools denied.
- `VERIFYING`: collecting enrollment/verification evidence; protected tools denied.
- `UNLOCKED`: verified, unexpired session and valid presence signals; protected tools may pass additional action-level policy.
- `REVERIFY_REQUIRED`: stale/low-confidence identity evidence; protected tools denied until verification succeeds.

On sensor disconnect, explicit lock, or clear removal signal: transition immediately to `LOCKED` (subject to actual detection latency). Periodic re-verification is a stretch goal, not a substitute for removal handling.

## Minimal API / contracts (adapt to stack)
```ts
type AuthDecision = {
  allowed: boolean;
  reason: string;
  sessionId?: string;
  checkedAt: string;
};

// Called inside every protected tool wrapper, directly before the side effect.
async function authorizeToolCall(input: {
  userId: string;
  toolName: string;
  args: Record<string, unknown>;
  commandSpeakerVerified: boolean;
}): Promise<AuthDecision>;
```

**Demo tools:**
- `get_wallet_status` or `get_agent_status` — read-only, permitted while locked.
- `create_test_payment(amount, merchantId)` — protected by Clasp; test mode only, with a small spending cap and known demo merchant.

## Build order — prioritize end-to-end proof
**P0 — Hardware truth:** Connect Vexo, observe real audio and motion/tap events; connect Polar, confirm whether raw PPG streaming and disconnect detection work. Document what's actually available.

**P1 — Protected tool:** Build a deterministic Clasp gateway around one Stripe test-mode operation. Prove `UNLOCKED -> allow` and `LOCKED -> deny` using a manual state toggle **before** integrating biometrics. The toggle must be clearly labeled a development fallback, not presented as biometric verification.

**P2 — Agent:** Connect Mastra to an LLM and route a spoken/text command to the protected tool. Ensure denial is enforced outside the prompt/model.

**P3 — Wearable state:** Replace manual state with validated Vexo/Polar presence and a real authentication flow. Enrollment and identity matching can be a clearly labeled experimental prototype.

**P4 — Demo polish:** Live audit dashboard, Vexo LED feedback if supported, optional ElevenLabs voice response.

## Demo script (60–90 seconds)
1. **Problem:** “AI agents have your permissions. But how do they know it's still you?”
2. **Authenticated owner:** Show valid wearable identity session. Say: “Pay five dollars to our demo merchant.” Agent requests tool; Clasp allows; Stripe test transaction appears.
3. **Hand-off/removal:** Remove/disconnect sensor or transfer wearable; dashboard shows lock/revocation. Have another person issue the same request. Agent proposes the tool; Clasp denies; Stripe receives **no** transaction.
4. **Proof:** Show tool-call audit with policy decision, reason, and external-call count.
5. **Vision:** Clasp can mediate any high-impact agent tool, not just payments.

## Scope control
**Must ship:** agent tool request, deterministic tool-level enforcement, allow/deny demo, wearable-derived state, audit UI.

**Nice to have:** voiceprint, raw PPG identity classifier, periodic re-verification, ElevenLabs, LED status, multiple protected integrations.

**Avoid:** real payments, production security claims, custom hardware, Raspberry Pi, trying every sponsor API, or building a general-purpose memory assistant.

## Unknowns to resolve, not assume
- Exact Vexo Open Kit audio, motion, gesture, and LED interfaces and whether they can run simultaneously.
- Polar Verity Sense raw PPG BLE access on this device/firmware; connection stability; repeatability across people and rewearing.
- Whether the selected Gemma endpoint works smoothly with Mastra's tool-calling requirements.
- Exact Stripe demo flow (use a known test merchant; never infer a recipient from “pay for coffee”).

## Working conventions for coding assistants
- Prefer the **smallest working vertical slice** over elaborate abstractions.
- Make one concrete implementation choice and execute it; avoid option sprawl.
- Mark mocked or simulated inputs explicitly in code, logs, and demo UI.
- Do not invent SDK methods or API shapes: inspect docs, examples, or source first.
- Use environment variables for secrets; never commit `.env`.
- Make errors observable and all sensitive actions fail closed.
- Prioritize a reliable live demo over speculative ML accuracy.
