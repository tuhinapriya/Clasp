# Clasp

**Your identity. Your AI. Your control.**

Clasp is an identity-bound authorization layer for AI agents. An agent can understand a request and ask to use a sensitive tool, but Clasp makes the authorization decision in application code before the tool runs.

For the demo, Clasp combines voice verification from a Vexo wearable with pulse-wave verification from a Polar Verity Sense. The result is a hands-free identity check at the moment an agent wants to act.

## How it works

1. **Speak to your agent.** The Vexo microphone captures your request. Clasp transcribes it locally and compares your voice with your enrolled speaker profile.
2. **Verify your pulse.** The Polar sensor streams PPG and motion data. Clasp checks signal quality and compares pulse embeddings with your enrolled profile.
3. **Request an action.** The Mastra agent uses Gemini to interpret the request and decide whether to call a tool.
4. **Check the boundary.** Clasp evaluates both identity factors and the tool's policy. A missing, mismatched, uncertain, or disconnected factor blocks the action.
5. **Execute and display the result.** If authorized, the protected tool creates a Stripe **test-mode** payment. The dashboard shows the request, identity checks, decision, and result.

```text
Vexo microphone ──► Voice verification ──┐
                                         ├──► Clasp authorization ──► Stripe TEST
Polar PPG + motion ─► Pulse verification ─┘             ▲
                                               Gemini / Mastra agent
```

The gateway—not the language model—controls tool execution. Stripe credentials stay on the server, and duplicate requests use an idempotency key. Voice and PPG processing and profiles are local to the Mac.

## The idea behind cardiac biometrics

Cardiac signals contain physiological patterns that researchers have studied for biometric recognition. ECG captures the heart's electrical activity; PPG measures changes in blood volume optically. Pulse shape, timing, and other waveform features can be used to build and compare biometric representations. Clasp explores this idea using **PPG**, alongside voice; it does not collect ECG.

- [Biometric Recognition: A Systematic Review on Electrocardiogram Data Acquisition Methods](https://pmc.ncbi.nlm.nih.gov/articles/PMC9921530/)
- [PPG-based biometric authentication: A review on architectures, datasets, attacks and security challenges](https://pmc.ncbi.nlm.nih.gov/articles/PMC12664393/)

These papers motivate the approach; they do not validate this specific implementation. Clasp's PPG matching is experimental, and its demo thresholds require independent evaluation before security-sensitive use.

## Built with

- **Agent:** Mastra and Google Gemini
- **Voice:** Vexo Open Kit microphone, local Whisper transcription, and SpeechBrain ECAPA-TDNN speaker embeddings
- **Pulse:** Polar Verity Sense PPG and accelerometer data, with Pulse-PPG embeddings
- **Authorization:** TypeScript policy gateway and audit events
- **Payments:** Stripe test-mode PaymentIntents
- **Interface:** Local web dashboard with live updates

## Run locally

Clasp is designed for macOS with a Vexo band and Polar Verity Sense. Install Node.js, Python, and Bluetooth dependencies, then from the project directory:

```bash
git clone https://github.com/VexoAI-Org/vexo-open-kit.git vexo-open-kit
npm install
npm run setup:vexo
npm run setup:voice
cp .env.example .env
```

Add `GOOGLE_API_KEY` and a Stripe **test secret key** (`sk_test_...`) to `.env`. Install the Polar module dependencies and Pulse-PPG source and checkpoint using the instructions in [clasp-ppg/README.md](clasp-ppg/README.md). The local PPG worker also needs a profile-specific experimental threshold configuration to produce a match; profiles and calibration data are not included in the repository.

```bash
npm run dev
```

Open the [demo](http://127.0.0.1:4173/demo.html) to connect the devices and speak to the agent. Use the [Identity dashboard](http://127.0.0.1:4173/identity.html) to register or re-register voice and pulse profiles. PPG registration captures approximately 90 seconds of sensor data.

Run the checks with:

```bash
npm run check
npm test
cd clasp-ppg && .venv/bin/python -m pytest -q
```
