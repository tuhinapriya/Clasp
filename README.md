# Clasp

Clasp checks authorization in application code immediately before a sensitive tool executes. The agent can request a payment; it cannot authorize one.

## Local setup

Clone the Vexo reference kit beside Clasp (the kit's README recommends keeping it separate from your repository), then install the Node and BLE dependencies:

```bash
git clone https://github.com/VexoAI-Org/vexo-open-kit.git vexo-open-kit
npm install
npm run setup:vexo
npm run setup:voice
cp .env.example .env
# Set GOOGLE_API_KEY and STRIPE_SECRET_KEY in .env
npm run dev
```

Open <http://127.0.0.1:4173>. The dashboard and API bind to `127.0.0.1`. The Vexo band number is defined once in `src/hardware/vexo-config.ts` as 78.

## Real integrations and their limits

- **Mastra + Google:** `MODEL_ID` defaults to `google/gemini-3.8-flash`. With `GOOGLE_API_KEY` set, every chat request goes to that model and returns `usedRealLlm: true` plus its model ID. Without a key, Clasp rejects the request with HTTP 503; there is no local parser fallback. A configured key/model is not evidence that a provider request succeeded; the dashboard shows request-level results and errors.
- **Stripe:** Set `STRIPE_SECRET_KEY` to an `sk_test_` key only. Clasp refuses any other key. It creates and confirms test PaymentIntents using Stripe's `pm_card_visa` test payment method; no real money can move. The amount limit is $5 and the merchant ID defaults to `clasp_demo_merchant`.
- **Vexo:** The Node server launches `scripts/vexo-bridge.py`, which imports the local, unmodified `vexo-open-kit/python` SDK and calls the documented `Band.connect(78)` Bluetooth API. The SDK checks the device-reported band number before returning. Motion, gesture, link, and microphone events are telemetry only. A bridge heartbeat expires after five seconds; disconnection invalidates voice evidence, but connectivity never counts as identity verification.
- **Voice speaker verification:** `npm run setup:voice` installs local SpeechBrain ECAPA-TDNN and MLX Whisper support. Clasp captures the Vexo Opus microphone stream, decodes it to 16 kHz mono PCM with the kit's `PcmDecoder`, checks speech quality, and computes an ECAPA embedding locally. Use **Register voice** to enroll; replacement requires explicit confirmation. Normal Vexo voice turns use the same captured PCM for speaker matching and local transcription. Audio is processed in memory and is not saved. Embeddings are stored locally at `.clasp/voice-profile.json` with restrictive file permissions. A result is `MATCH`, `MISMATCH`, or `UNCERTAIN` and expires after 15 seconds; missing profiles, unusable audio, disconnected sources, and uncertain scores fail closed. This has not yet been validated against the owner's live voice, so the match threshold needs real enrollment/validation review.
- **Biometric authorization:** In the default `CLASP_IDENTITY_MODE=biometric`, a payment requires fresh real `MATCH` results from both the Vexo voice-speaker verifier and the Polar PPG identity verifier for the same enrolled subject. The Polar worker runs Pulse-PPG and morphology; without independently calibrated thresholds it returns UNCERTAIN, so real biometric payment authorization remains locked. Missing, stale, mismatched, uncertain, simulated, or disconnected factors deny and lock authorization. Verifier contracts are in `src/identity/biometrics.ts`. Connectivity, transcription, PPG samples, and pulse detection are never treated as identity matches.
- **Development simulation:** Set `CLASP_IDENTITY_MODE=development-simulation` to expose the explicit dashboard simulation control for local test-only flows. This provider is separate from biometric evidence; simulated results cannot enter the biometric verifier store.

## Dashboard demo

In biometric mode, connect actual voice-speaker and Polar PPG verifier adapters first. Their independent fresh match results automatically create the short-lived identity session. Then ask the real agent to make a permitted payment; the tool rechecks identity evidence and policy immediately before Stripe. No gesture confirmation is required.

For an explicitly simulated local test, set `CLASP_IDENTITY_MODE=development-simulation`, restart Clasp, activate the labeled simulation control, and ask for a permitted test payment. This mode does not create or impersonate biometric matches.

`npm run demo` checks service configuration and prints these steps. It does not fabricate sensor events or create payments automatically.

## Verification

```bash
npm run check
npm test
```

Tests cover locked/expired denial, revocation, payment policy, denial before the payment gateway, duplicate request idempotency, both-factor matching, single-factor failure, expiry, disconnect, mismatch, uncertainty, limit rejection, and voice enrollment/validation behavior. The ECAPA model has loaded locally, but actual Vexo audio enrollment and speaker matching still require a completed live recording. Live biometric payment authorization remains unavailable until PPG thresholds are independently calibrated. Dashboard telemetry and audit history are in memory and clear when the server restarts.

## Identity dashboard

Open <http://127.0.0.1:4173/identity.html> to manage the single `primary-user` voice and PPG profiles. Voice registration and testing use the Vexo microphone. The Polar registration button starts an explicit approximately 90-second capture; all nine synchronized 10-second windows must pass the existing signal and motion gates before a profile is saved. Re-registration requires confirmation and does not replace a profile if capture or quality checks fail. After enrollment, use **Start live verification** to reconnect the Polar worker and begin continuous checks. Enrollment alone never authenticates.

The default local Pulse-PPG source path is `.clasp/pulseppg-source`; its official checkpoint is `.clasp/pulseppg-checkpoint.pkl`. Override these using `CLASP_PULSEPPG_SOURCE` and `CLASP_PULSEPPG_CHECKPOINT`. See `clasp-ppg/README.md` for the isolated Python environment, model dependencies, and experimental limitations. PPG similarity is not calibrated for secure authentication; placement experiments showed genuine and impostor score overlap. The last successful authentication time is held in server memory and resets when Clasp restarts.
