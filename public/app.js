import { createVexoController } from '/vexo.js';

const $ = (selector) => document.querySelector(selector);
const state = { eventCount: 0, auditIds: new Set(), stream: null, bandNumber: null, vexoSdkReady: false, voiceProfile: null, voiceCaptureActive: false };
const fmtTime = (iso) => new Date(iso).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
const toast = (message) => { const el = $('#toast'); el.textContent = message; el.classList.add('show'); setTimeout(() => el.classList.remove('show'), 2600); };
const vexo = createVexoController({ onReady: ({ available, reason }) => { state.vexoSdkReady = available; $('#vexo-connect').disabled = !available || state.bandNumber === null; if (!available) $('#vexo-message').textContent = reason; }, onMessage: (kind, message) => { if (kind === 'connected' || kind === 'disconnected' || kind === 'microphone-on' || kind === 'microphone-off' || kind === 'error') toast(message); } });

function applyIdentity(identity) {
  const pill = $('#state-pill');
  pill.className = `state-pill ${identity.state.toLowerCase()}`;
  $('#identity-state').textContent = identity.state;
  $('#provider-name').textContent = identity.provider ?? 'No active session';
  $('#expires').textContent = identity.expiresAt ? new Date(identity.expiresAt).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' }) : '—';
  $('#identity-visual').classList.toggle('is-authenticated', identity.state === 'AUTHENTICATED');
  $('#core-icon').textContent = identity.state === 'AUTHENTICATED' ? '✓' : '⌑';
  if (identity.simulated === true) $('#identity-mode-badge').innerHTML = '<span>◌</span> SIMULATED TEST MODE';
}

function applyBiometrics(data) {
  if (!data) return;
  $('#voice-factor').textContent = data.voiceSpeaker?.status ?? 'UNAVAILABLE';
  $('#polar-factor').textContent = data.polarPpg?.status ?? 'UNAVAILABLE';
  const status = data.authorized === true
    ? ' Both fresh verifier results match the same identity.'
    : ' Both real verifier matches required · connectivity and raw signals are not identity proof.';
  $('#identity-foot').lastChild.textContent = status;
  const voice = data.voiceSpeaker;
  if (voice?.similarity != null) $('#voice-score').textContent = `Similarity ${Number(voice.similarity).toFixed(3)} · expires ${voice.expiresAt ? new Date(voice.expiresAt).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' }) : '—'}`;
}

function applyVoiceProfile(profile) {
  state.voiceProfile = profile ?? null;
  const button = $('#voice-register');
  button.textContent = profile?.registered ? 'Re-enroll voice' : 'Start recording';
  $('#voice-enrollment').textContent = profile?.registered ? `Registered identity: ${profile.identityId}` : 'No voice profile registered.';
  $('#voice-test').disabled = !profile?.registered || !$('#vexo-state').classList.contains('device-connected') || state.voiceCaptureActive;
  button.disabled = !$('#vexo-state').classList.contains('device-connected') || state.voiceCaptureActive;
  if (profile?.registered) $('#voice-score').textContent = `Profile · ${String(profile.identityId).slice(0, 8)} · ${profile.enrolledAt ? new Date(profile.enrolledAt).toLocaleDateString() : 'enrolled'}`;
}

function applyVoiceEvent(event) {
  const d = event.data ?? {};
  if (event.type === 'voice.profile') applyVoiceProfile(d);
  if (event.type === 'voice.status' && d.status === 'loading') $('#voice-status').textContent = `Loading pretrained ${d.model} locally; first run may take a minute.`;
  if (event.type === 'voice.test.completed') { state.voiceCaptureActive = false; $('#voice-test-result').textContent = `${d.decision} · similarity ${d.similarity ?? 'unavailable'} · speech ${d.speechSeconds ?? 'unknown'} seconds · ${d.reason ?? ''}`; applyVoiceProfile(state.voiceProfile); $('#voice-talk').disabled = false; $('#voice-stop').disabled = true; }
  if (event.type === 'voice.capture.started') {
    $('#voice-stop').disabled = false; $('#voice-progress').textContent = `Vexo microphone recording for up to ${d.durationSeconds} seconds. Read aloud now.`;
    state.voiceCaptureActive = true;
    $('#voice-talk').disabled = true; $('#voice-register').disabled = true;
    $('#voice-status').textContent = d.kind === 'registration' ? 'Recording enrollment speech from Vexo for 10 seconds. Speak naturally.' : 'Listening on Vexo for up to 8 seconds. Speak your request naturally.';
  }
  if (event.type === 'voice.registration.completed') {
    state.voiceCaptureActive = false; $('#voice-stop').disabled = true; $('#voice-progress').textContent = `Profile saved. Usable speech: ${d.speechSeconds ?? 'unknown'} seconds.`;
    $('#voice-status').textContent = `Voice profile registered locally for ${d.profile?.identityId ?? 'your owner identity'}. No recording was saved.`;
    applyVoiceProfile(d.profile);
  }
  if (event.type === 'voice.registration.failed' || event.type === 'voice.capture.failed' || event.type === 'voice.error') {
    state.voiceCaptureActive = false;
    $('#voice-stop').disabled = true; $('#voice-progress').textContent = d.reason ?? 'Voice operation failed.'; $('#voice-test-result').textContent = d.reason ?? 'Voice operation failed.';
    $('#voice-status').textContent = d.reason ?? 'Voice operation failed.';
    applyVoiceProfile(state.voiceProfile);
    $('#voice-talk').disabled = !$('#vexo-state').classList.contains('device-connected');
  }
  if (event.type === 'identity.voice_verification') {
    const score = d.similarity == null ? 'score unavailable' : `cosine similarity ${Number(d.similarity).toFixed(3)}`;
    $('#voice-status').textContent = `Speaker ${d.decision} · ${score} · ${d.reason ?? ''}`;
  }
  if (event.type === 'voice.transcription') {
    state.voiceCaptureActive = false;
    $('#voice-talk').disabled = !$('#vexo-state').classList.contains('device-connected');
    if (d.text?.trim()) {
      $('#voice-status').textContent = `Local transcript ready · speaker ${d.speakerDecision}. Sending the transcript to Clasp agent.`;
      sendRequest(d.text, `voice_${d.turnId}`);
    } else {
      $('#voice-status').textContent = d.reason ?? 'No usable speech was transcribed; try again.';
    }
    applyVoiceProfile(state.voiceProfile);
  }
}

function eventDescription(event) {
  const d = event.data ?? {};
  if (event.type === 'audit.entry') {
    const action = d.requestedAction ?? {};
    const amount = action.amountUsd != null ? `$${Number(action.amountUsd).toFixed(2)}` : '';
    return `${amount} ${action.tool ?? ''} · ${d.reason ?? ''}`.trim();
  }
  if (event.type === 'authorization.decision') return `${d.tool ?? 'tool'} · ${d.reason ?? ''}`;
  if (event.type === 'agent.tool.requested' || event.type === 'agent.tool.proposed') return `${d.tool ?? 'payment tool'} requested${d.amountUsd != null ? ` · $${Number(d.amountUsd).toFixed(2)}` : ''}`;
  if (event.type === 'identity.biometric_factors') return `Voice speaker: ${d.voiceSpeaker}; Polar PPG identity: ${d.polarPpg} · ${d.reason ?? ''}`;
  if (event.type === 'identity.voice_verification') return `Speaker ${d.decision} · ${d.similarity == null ? 'similarity unavailable' : `cosine ${Number(d.similarity).toFixed(3)}`} · ${d.reason ?? ''}`;
  if (event.type.startsWith('identity.')) return d.reason ?? event.type.replace('identity.', 'Identity ');
  if (event.type === 'tool.execution.completed') return `${d.tool ?? 'tool'} · ${d.reason ?? d.paymentIntent?.status ?? (d.ok ? 'completed' : 'blocked')}`;
  if (event.type === 'agent.request.received') return 'Agent request received';
  if (event.type === 'hardware.vexo.connection') return `${d.name ?? 'Vexo'} · ${d.detail ?? d.status}`;
  if (event.type === 'hardware.vexo.motion') return `Live accelerometer ${d.motion?.ax ?? '—'}, ${d.motion?.ay ?? '—'}, ${d.motion?.az ?? '—'} g`;
  if (event.type === 'hardware.vexo.gesture') return `Vexo gesture · ${d.gesture}`;
  if (event.type === 'hardware.vexo.audio') return `Microphone packets received · ${d.packets} packets`;
  if (event.type === 'hardware.vexo.link') return `Bluetooth signal · ${d.rssi ?? '—'} dBm`;
  if (event.type === 'agent.request.rejected') return d.reason ?? 'Real model unavailable';
  if (event.type === 'agent.response') return 'Agent response ready';
  if (event.type === 'agent.error') return d.message ?? 'Agent request failed';
  return event.type;
}

function eventStatus(event) {
  const d = event.data ?? {};
  if (event.type === 'audit.entry') return d.decision === 'ALLOW' ? (d.executionOutcome === 'succeeded' ? 'SUCCEEDED' : 'ALLOWED') : 'BLOCKED';
  if (event.type === 'authorization.decision') return d.allowed ? 'ALLOWED' : 'DENIED';
  if (event.type === 'identity.voice_verification') return d.decision ?? 'UNCERTAIN';
  if (event.type === 'identity.authenticated') return 'AUTHENTICATED';
  if (event.type === 'identity.revoked') return 'REVOKED';
  if (event.type === 'identity.expired') return 'EXPIRED';
  if (event.type === 'agent.tool.requested' || event.type === 'agent.tool.proposed') return 'REQUESTED';
  if (event.type === 'tool.execution.completed') return d.decision === 'DENY' ? 'BLOCKED' : (d.ok ? (d.paymentIntent?.status ?? 'DONE') : 'NOT RUN');
  if (event.type === 'agent.error' || event.type === 'agent.request.rejected') return 'ERROR';
  if (event.type === 'hardware.vexo.connection') return d.status === 'connected' ? 'CONNECTED' : 'DISCONNECTED';
  if (event.type === 'hardware.vexo.gesture') return d.gesture === 'double-tap' ? 'GESTURE' : 'SENSOR';
  if (event.type === 'identity.biometric_factors') return d.authorized ? 'MATCHED' : 'LOCKED';
  return 'LOGGED';
}

function eventIcon(event) {
  if (event.type.startsWith('identity.')) return '◉';
  if (event.type.includes('authorization') || event.type === 'audit.entry') return '◇';
  if (event.type.includes('tool') || event.type.includes('agent')) return '✳';
  if (event.type.startsWith('hardware.vexo')) return '⌁';
  return '·';
}

function addEvent(event, prepend = true) {
  if (!event?.id || state.auditIds.has(event.id)) return;
  state.auditIds.add(event.id);
  state.eventCount++;
  $('#event-count').textContent = `${state.eventCount} event${state.eventCount === 1 ? '' : 's'}`;
  const empty = $('.activity-empty');
  if (empty) empty.remove();
  const row = document.createElement('div');
  row.className = 'activity-row';
  const eventName = event.type === 'audit.entry' ? 'Audit record' : event.type.replaceAll('.', ' ');
  const status = eventStatus(event);
  const name = document.createElement('span'); name.className = 'event-name';
  const mark = document.createElement('i'); mark.className = 'event-mark'; mark.textContent = eventIcon(event);
  name.append(mark, document.createTextNode(eventName));
  const detail = document.createElement('span'); detail.className = 'event-detail'; detail.title = eventDescription(event); detail.textContent = eventDescription(event);
  const time = document.createElement('span'); time.className = 'event-time'; time.textContent = fmtTime(event.at);
  const badge = document.createElement('span'); badge.className = `event-status ${status.toLowerCase()}`; badge.textContent = status;
  row.append(name, detail, time, badge);
  $('#activity-list').insertBefore(row, $('#activity-list').firstChild);
  while ($('#activity-list').children.length > 40) $('#activity-list').lastElementChild.remove();
  if (event.type === 'identity.authenticated') applyIdentity({ state: 'AUTHENTICATED', provider: event.data.provider, expiresAt: event.data.expiresAt });
  if (event.type === 'identity.revoked') applyIdentity({ state: 'LOCKED', provider: null, expiresAt: null });
  if (event.type === 'identity.expired') applyIdentity({ state: 'EXPIRED', provider: null, expiresAt: null });
  if (event.type === 'tool.execution.completed') updatePaymentResult(event);
  if (event.type.startsWith('hardware.vexo.')) applyHardwareEvent(event);
  if (event.type === 'identity.biometric_factors') applyBiometrics(event.data);
  if (prepend && (event.type.startsWith('voice.') || event.type === 'identity.voice_verification')) applyVoiceEvent(event);
}

function updatePaymentResult(event) {
  const data = event.data ?? {};
  const intent = data.paymentIntent;
  const title = $('#payment-result-title');
  const detail = $('#payment-result-detail');
  if (intent) {
    title.textContent = `${(intent.currency ?? 'usd').toUpperCase()} ${(Number(intent.amount ?? 0) / 100).toFixed(2)} · ${String(intent.status).toUpperCase()}`;
    detail.textContent = `Stripe test PaymentIntent ${intent.id} · livemode: false`;
  } else if (data.decision === 'DENY') {
    title.textContent = 'BLOCKED · Stripe was not called';
    detail.textContent = data.reason ?? 'No authenticated identity session.';
  } else if (data.stripeCalled === false) {
    title.textContent = 'Authorized · Stripe was not called';
    detail.textContent = data.reason ?? 'Stripe test credentials are not configured.';
  }
}

function applyVexoState(device) {
  if (!device) return;
  const connected = device.status === 'connected';
  const statusDot = document.createElement('i');
  $('#vexo-state').replaceChildren(statusDot, document.createTextNode(connected ? ' CONNECTED' : ' NOT CONNECTED'));
  $('#vexo-state').classList.toggle('device-connected', connected);
  $('#vexo-live-detail').textContent = connected ? (device.microphoneOn ? 'Mic live · motion live' : 'Motion and gestures live · mic off') : (device.detail ?? 'Waiting for connection');
  const connecting = device.status === 'unknown';
  $('#vexo-connect').hidden = connected;
  $('#vexo-connect').disabled = connecting || !state.vexoSdkReady;
  $('#vexo-disconnect').hidden = !connected && !connecting;
  $('#vexo-mic').disabled = !connected;
  $('#vexo-mic').textContent = device.microphoneOn ? 'Stop microphone' : 'Start microphone';
  $('#voice-talk').disabled = !connected || state.voiceCaptureActive;
  applyVoiceProfile(state.voiceProfile);
  if (connected && device.name) $('#vexo-device-detail').textContent = `${device.name} · Bluetooth · verified by SDK`;
  else if (state.bandNumber) $('#vexo-device-detail').textContent = `Vexo Open Kit · Bluetooth · band ${state.bandNumber}`;
}

function applyHardwareEvent(event) {
  if (event.type === 'hardware.vexo.connection') {
    applyVexoState({ status: event.data.status, detail: event.data.detail, name: event.data.name, microphoneOn: false });
  }
  if (event.type === 'hardware.vexo.microphone') {
    $('#vexo-live-detail').textContent = event.data.active ? 'Mic telemetry live · not speaker verification' : 'Motion live · mic off';
    $('#vexo-mic').textContent = event.data.active ? 'Stop microphone' : 'Start microphone';
  }
  if (event.type === 'hardware.vexo.motion') $('#vexo-live-detail').textContent = `Motion live · z ${event.data.motion?.az ?? '—'} g · mic ${$('#vexo-mic').textContent === 'Stop microphone' ? 'on' : 'off'}`;
}

function chatBubble(text, kind) {
  const el = document.createElement('div');
  el.className = `chat-bubble ${kind}`;
  el.textContent = text;
  if ($('#thread-empty')) $('#thread-empty').remove();
  $('#chat-thread').append(el);
  $('#chat-thread').scrollTop = $('#chat-thread').scrollHeight;
  return el;
}

async function loadState() {
  const response = await fetch('/api/state');
  const data = await response.json();
  applyIdentity(data.identity);
  applyBiometrics(data.biometrics);
  applyVoiceProfile(data.voiceProfile);
  const simulatedMode = data.identityMode === 'development-simulation';
  $('#authenticate').hidden = !simulatedMode;
  $('#identity-mode-badge').innerHTML = simulatedMode ? '<span>◌</span> SIMULATED TEST MODE' : '<span>◉</span> BIOMETRIC MODE';
  if (simulatedMode) $('#identity-foot').lastChild.textContent = ' Development-only simulation · isolated from biometric results.';
  $('#agent-mode').textContent = data.agent.configured ? `REAL LLM · ${data.agent.model}` : 'REAL LLM UNAVAILABLE';
  $('#model-detail').textContent = data.agent.configured ? `Mastra agent · Google model configured: ${data.agent.model}. Each chat response reports whether this request used the real LLM.` : `Mastra agent · ${data.agent.model} is selected but GOOGLE_API_KEY is missing. Requests are rejected; there is no local parser fallback.`;
  state.bandNumber = data.hardware.vexo.expectedBandNumber;
  $('#vexo-device-detail').textContent = `Vexo Open Kit · Bluetooth · band ${state.bandNumber}`;
  $('#vexo-connect').textContent = `Connect band ${state.bandNumber}`;
  $('#vexo-connect').disabled = !state.vexoSdkReady;
  applyVexoState(data.hardware.vexo);
  $('#payment-limit').textContent = '$5.00 · USD · demo merchant';
  $('#today-date').textContent = new Date().toLocaleDateString([], { weekday: 'long', month: 'long', day: 'numeric', year: 'numeric' }).toUpperCase();
  for (const event of data.events ?? []) {
    addEvent(event, false);
    if (event.type === 'tool.execution.completed') updatePaymentResult(event);
    if (event.type.startsWith('hardware.vexo.')) applyHardwareEvent(event);
    if (event.type === 'identity.biometric_factors') applyBiometrics(event.data);
  }
  state.eventCount = (data.events ?? []).length;
  $('#event-count').textContent = `${state.eventCount} event${state.eventCount === 1 ? '' : 's'}`;
  if (!data.stripe.configured) toast('Stripe test key not set — allowed requests will be recorded without contacting Stripe.');
}

function connectEvents() {
  state.stream = new EventSource('/api/events');
  state.stream.addEventListener('snapshot', (message) => {
    const snapshot = JSON.parse(message.data);
    applyIdentity(snapshot.identity);
    applyBiometrics(snapshot.biometrics);
    applyVoiceProfile(snapshot.voiceProfile);
    applyVexoState(snapshot.hardware?.vexo);
    for (const event of snapshot.recent ?? []) addEvent(event, false);
  });
  state.stream.addEventListener('clasp', (message) => { const event = JSON.parse(message.data); addEvent(event); });
  state.stream.onerror = () => { $('#agent-mode').dataset.connection = 'reconnecting'; };
}

async function post(path, body = {}) {
  const response = await fetch(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  const result = await response.json();
  if (!response.ok) throw new Error(result.error ?? 'Request failed');
  return result;
}

$('#authenticate').addEventListener('click', async () => {
  try { const identity = await post('/api/identity/authenticate'); applyIdentity(identity); toast('Simulated identity session active for 2 minutes.'); }
  catch (error) { toast(error.message); }
});
for (const id of ['#revoke', '#revoke-top']) $(id).addEventListener('click', async () => {
  try { const identity = await post('/api/identity/revoke'); applyIdentity(identity); toast('Session revoked. Sensitive tools are locked.'); }
  catch (error) { toast(error.message); }
});

async function sendRequest(text, requestId = crypto.randomUUID()) {
  if (!text.trim()) return;
  chatBubble(text, 'user');
  const pending = chatBubble('Checking request…', 'agent pending');
  try {
    const result = await post('/api/agent/message', { text, requestId });
    pending.textContent = `${result.answer} · ${result.usedRealLlm ? `real LLM: ${result.model}` : 'real LLM not used'}`;
    pending.classList.remove('pending');
  } catch (error) {
    pending.textContent = `Agent unavailable: ${error.message} No local parser was used.`;
    pending.classList.remove('pending');
  }
}
$('#vexo-connect').addEventListener('click', () => {
  if (!state.bandNumber) return toast('Vexo band number is not configured.');
  const operation = vexo.connect(state.bandNumber); // must begin synchronously from the click for Web Bluetooth
  $('#vexo-connect').disabled = true;
  $('#vexo-message').textContent = `Choose Vexo-HACK-${String(state.bandNumber).padStart(2, '0')} in the Chrome or Edge device picker.`;
  operation.then((result) => { $('#vexo-message').textContent = result.message ?? 'Waiting for the real Vexo SDK connection event…'; })
    .catch((error) => { $('#vexo-connect').disabled = false; $('#vexo-message').textContent = error.message; });
});
$('#vexo-disconnect').addEventListener('click', () => vexo.disconnect().catch((error) => toast(error.message)));
$('#vexo-mic').addEventListener('click', () => {
  const active = $('#vexo-mic').textContent === 'Stop microphone';
  const operation = active ? vexo.stopMicrophone() : vexo.startMicrophone();
  operation.catch((error) => toast(error.message));
});

$('#voice-register').addEventListener('click', async () => {
  const replace = state.voiceProfile?.registered === true;
  if (replace && !window.confirm('Re-enroll your voice? This replaces the existing local speaker embedding for this identity.')) return;
  state.voiceCaptureActive = true;
  $('#voice-register').disabled = true;
  $('#voice-talk').disabled = true;
  $('#voice-status').textContent = 'Preparing local ECAPA speaker model and Vexo capture…';
  try {
    const result = await post('/api/voice/register', { replace });
    $('#voice-status').textContent = result.message;
  } catch (error) {
    state.voiceCaptureActive = false;
    applyVoiceProfile(state.voiceProfile);
    $('#voice-talk').disabled = !$('#vexo-state').classList.contains('device-connected');
    $('#voice-status').textContent = error.message;
  }
});

$('#voice-stop').addEventListener('click', () => post('/api/voice/stop', {}).catch(error => toast(error.message)));
$('#voice-test').addEventListener('click', async () => {
  state.voiceCaptureActive = true; applyVoiceProfile(state.voiceProfile); $('#voice-talk').disabled = true;
  $('#voice-test-result').textContent = 'Preparing Vexo microphone…';
  try { await post('/api/voice/test', {}); } catch (error) { state.voiceCaptureActive = false; applyVoiceProfile(state.voiceProfile); $('#voice-test-result').textContent = error.message; }
});

$('#voice-talk').addEventListener('click', async () => {
  const turnId = crypto.randomUUID();
  state.voiceCaptureActive = true;
  $('#voice-talk').disabled = true;
  $('#voice-register').disabled = true;
  $('#voice-status').textContent = 'Preparing Vexo microphone and local speaker verification…';
  try {
    const result = await post('/api/voice/talk', { turnId });
    $('#voice-status').textContent = result.message;
  } catch (error) {
    state.voiceCaptureActive = false;
    applyVoiceProfile(state.voiceProfile);
    $('#voice-talk').disabled = !$('#vexo-state').classList.contains('device-connected');
    $('#voice-status').textContent = error.message;
  }
});

$('#chat-form').addEventListener('submit', (event) => {
  event.preventDefault();
  const input = $('#chat-input');
  const value = input.value;
  input.value = '';
  sendRequest(value);
});
document.querySelectorAll('.suggestion').forEach((button) => button.addEventListener('click', () => sendRequest(button.dataset.prompt)));
$('#clear-activity').addEventListener('click', () => {
  $('#activity-list').replaceChildren();
  const empty = document.createElement('div');
  empty.className = 'activity-empty';
  const symbol = document.createElement('span'); symbol.textContent = '◌';
  const title = document.createElement('strong'); title.textContent = 'View cleared';
  const note = document.createElement('small'); note.textContent = 'New events will appear here.';
  empty.append(symbol, title, note);
  $('#activity-list').append(empty);
  toast('Activity view cleared. Server audit history remains available until restart.');
});
$('.notice-close').addEventListener('click', () => $('.notice').remove());

function tick() { $('#clock').textContent = new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }); }
tick(); setInterval(tick, 30_000);
loadState().then(connectEvents).catch(() => toast('Could not connect to Clasp server.'));
