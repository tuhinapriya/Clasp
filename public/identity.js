const $ = (id) => document.getElementById(id);
let state = null;
let voiceBusy = false;
let polarBusy = false;
let polarEnrolling = false;
let voiceCaptureStarted = 0;
let voiceDuration = 0;
let latestVoiceTest = null;
let refreshTimer = null;
function scheduleRefresh() { if (!refreshTimer) refreshTimer = setTimeout(() => { refreshTimer = null; refresh().catch(() => {}); }, 200); }
const time = (value) => value ? new Date(value).toLocaleTimeString() : '—';
const fresh = (expires) => !expires ? 'no evidence' : Date.parse(expires) <= Date.now() ? 'expired' : `expires ${time(expires)}`;
const score = (value) => typeof value === 'number' ? value.toFixed(3) : '—';

function render(data) {
  state = data;
  const simulation = data.identityMode === 'development-simulation';
  $('combined-status').textContent = data.identity.state === 'AUTHENTICATED' ? (data.identity.simulated ? 'SIMULATED' : 'AUTHENTICATED') : 'LOCKED';
  $('combined-status').classList.toggle('authenticated', data.identity.state === 'AUTHENTICATED' && !data.identity.simulated);
  $('combined-reason').textContent = data.identityView?.reason ?? 'Identity status unavailable.';
  $('last-auth').textContent = data.identityView?.lastAuthenticatedAt ? `Last authenticated ${time(data.identityView.lastAuthenticatedAt)}` : 'No successful authentication yet';
  const connected = data.hardware.vexo.status === 'connected';
  $('voice-device').textContent = connected ? 'Connected' : data.hardware.vexo.status === 'unknown' ? 'Connecting…' : 'Not connected';
  $('voice-mic').textContent = data.hardware.vexo.microphoneOn ? 'Active · Vexo' : 'Off';
  $('connect-vexo').disabled = connected || data.hardware.vexo.status === 'unknown';
  const vp = data.voiceProfile;
  $('voice-profile').textContent = vp.reEnrollmentRequired ? 'Re-registration required' : vp.registered ? 'Registered · primary-user' : 'Not registered';
  const voice = data.biometrics.voiceSpeaker;
  $('voice-result').textContent = voice.status;
  $('voice-freshness').textContent = `${score(voice.similarity)} · ${fresh(voice.expiresAt)}${voice.verifiedAt ? ` · checked ${time(voice.verifiedAt)}` : ''}`;
  $('voice-register').hidden = vp.registered;
  $('voice-reregister').hidden = !vp.registered;
  $('voice-register').disabled = $('voice-reregister').disabled = !connected || voiceBusy;
  $('voice-test').disabled = !connected || !vp.registered || vp.reEnrollmentRequired || voiceBusy;
  if (latestVoiceTest) $('voice-test-result').textContent = `Test ${latestVoiceTest.decision} · similarity ${score(latestVoiceTest.similarity)} · ${latestVoiceTest.reason ?? 'Completed.'}`;
  const pv = data.polarVerifier;
  $('polar-device').textContent = pv.device?.connected ? (pv.device.streaming ? 'Connected · streaming' : 'Connected · waiting for streams') : (pv.running ? 'Connecting / unavailable' : 'Not connected');
  $('polar-profile').textContent = pv.profile?.reEnrollmentRequired ? 'Re-registration required for Pulse-PPG' : pv.profile?.registered ? 'Registered · primary-user' : pv.profile?.existing ? 'Invalid profile · re-register' : 'Not registered';
  $('polar-result').textContent = data.biometrics.polarPpg.status;
  const last = pv.latestVerification;
  $('polar-freshness').textContent = `${score(last?.similarity)} · ${fresh(data.biometrics.polarPpg.expiresAt)}${last?.verifiedAt ? ` · checked ${time(last.verifiedAt)}` : ''}`;
  if (last?.quality) {
    const q = last.quality;
    $('polar-quality').textContent = `Morphology distance ${score(last.morphologyDistance)} · motion RMS ${typeof q.motion_dynamic_rms_mg === 'number' ? q.motion_dynamic_rms_mg.toFixed(1) + ' mg' : '—'} · ${last.reason || 'Quality passed'}`;
  } else $('polar-quality').textContent = 'No window yet';
  $('polar-register').hidden = !!pv.profile?.existing;
  $('polar-reregister').hidden = !pv.profile?.existing;
  $('polar-register').disabled = $('polar-reregister').disabled = simulation || pv.running || polarBusy;
  $('polar-toggle').disabled = simulation || polarBusy || polarEnrolling || (!pv.running && !pv.profile?.registered);
  $('polar-toggle').textContent = pv.running ? 'Stop live verification' : 'Start live verification';
  const history = $('polar-history'); history.replaceChildren();
  const windows = last?.history ?? [];
  if (!windows.length) { const p = document.createElement('p'); p.textContent = 'No verification windows yet.'; history.append(p); }
  for (const w of [...windows].reverse()) { const row = document.createElement('div'); row.className = 'history-row'; const left = document.createElement('span'); left.textContent = `${time(w.timestamp)} · ${w.quality_approved ? 'Usable' : 'Rejected'}`; const right = document.createElement('strong'); right.textContent = score(w.similarity); row.append(left,right); history.append(row); }
}
async function refresh() { const response = await fetch('/api/state'); if (!response.ok) throw Error('Server unavailable'); render(await response.json()); }
async function post(path, body = {}) { const response = await fetch(path, { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body) }); const result = await response.json(); if (!response.ok) throw Error(result.error ?? 'Request failed'); return result; }
async function action(button, work, errorTarget) { button.disabled = true; try { await work(); } catch (e) { $(errorTarget).textContent = e.message; } finally { await refresh().catch(() => {}); } }
$('connect-vexo').onclick = () => action($('connect-vexo'), () => post('/api/hardware/vexo/connect'), 'voice-progress');
async function registerVoice(replace) {
  if (replace && !confirm('Replace the existing voice profile with a new recording?')) return;
  voiceBusy = true; $('voice-progress').textContent = 'Preparing Vexo microphone…';
  try { await post('/api/voice/register', {replace}); } catch (e) { voiceBusy = false; $('voice-progress').textContent = e.message; }
  await refresh();
}
$('voice-register').onclick = () => registerVoice(false);
$('voice-reregister').onclick = () => registerVoice(true);
$('voice-test').onclick = async () => { voiceBusy = true; latestVoiceTest = null; $('voice-test-result').textContent = 'Preparing Vexo microphone…'; try { await post('/api/voice/test'); } catch(e) { voiceBusy = false; $('voice-test-result').textContent = e.message; } await refresh(); };
async function registerPolar(replace) {
  if (replace && !confirm('Replace the existing PPG profile with a new 90-second recording?')) return;
  polarBusy = true; polarEnrolling = true; $('polar-progress').value = 0; $('polar-progress-label').textContent = '0 / 90 sec'; $('polar-message').textContent = 'Preparing the Polar sensor and model…';
  try { await post('/api/hardware/polar/register', {replace}); } catch(e) { polarEnrolling = false; $('polar-message').textContent = e.message; } finally { polarBusy = false; await refresh(); }
}
$('polar-register').onclick = () => registerPolar(false);
$('polar-reregister').onclick = () => registerPolar(true);
$('polar-toggle').onclick = async () => { polarBusy = true; try { await post(state?.polarVerifier.running ? '/api/hardware/polar/stop' : '/api/hardware/polar/start'); } catch(e) { $('polar-message').textContent = e.message; } finally { polarBusy = false; await refresh(); } };
const stream = new EventSource('/api/events');
stream.addEventListener('clasp', (message) => {
  const event = JSON.parse(message.data), d = event.data ?? {};
  if (event.type === 'voice.capture.started') { voiceCaptureStarted = Date.now(); voiceDuration = d.durationSeconds; $('voice-progress').textContent = `Recording from Vexo · 0 / ${voiceDuration} seconds. Read the passage aloud.`; }
  if (event.type === 'voice.registration.completed') { voiceBusy = false; voiceCaptureStarted = 0; $('voice-progress').textContent = `Voice profile saved · ${d.speechSeconds ?? '—'} seconds of usable speech.`; }
  if (event.type === 'voice.test.completed') { voiceBusy = false; voiceCaptureStarted = 0; latestVoiceTest = d; $('voice-progress').textContent = 'Voice test finished. Your saved profile was not changed.'; }
  if (['voice.registration.failed','voice.capture.failed','voice.error'].includes(event.type)) { voiceBusy = false; voiceCaptureStarted = 0; $('voice-progress').textContent = d.reason ?? 'Voice capture failed.'; }
  if (event.type === 'hardware.vexo.connection' && d.status === 'disconnected') { voiceBusy = false; voiceCaptureStarted = 0; }
  if (event.type === 'hardware.polar.worker') {
    if (d.type === 'enrollment.progress') { polarEnrolling = true; $('polar-progress').value = d.seconds; $('polar-progress-label').textContent = `${d.seconds} / ${d.totalSeconds} sec`; $('polar-message').textContent = d.quality; }
    if (d.type === 'enrollment.completed') { polarEnrolling = false; $('polar-message').textContent = `PPG profile saved for primary-user · ${d.approvedWindows} quality-approved windows. Start live verification when ready.`; }
    if (d.type === 'enrollment.failed' || d.type === 'error') { polarEnrolling = false; $('polar-message').textContent = d.reason; }
    if (d.type === 'exited' && polarEnrolling) { polarEnrolling = false; $('polar-message').textContent = 'Polar enrollment ended before a profile was saved.'; }
  }
  if (event.type.startsWith('identity.') || event.type.startsWith('voice.') || event.type === 'hardware.vexo.connection' || event.type === 'hardware.vexo.microphone' || event.type === 'hardware.polar.worker') scheduleRefresh();
});
setInterval(() => { if (voiceCaptureStarted) $('voice-progress').textContent = `Recording from Vexo · ${Math.min(voiceDuration, Math.floor((Date.now()-voiceCaptureStarted)/1000))} / ${voiceDuration} seconds. Read the passage aloud.`; }, 1000);
setInterval(() => refresh().catch(() => { $('combined-reason').textContent = 'Connection to Clasp lost; authorization status unavailable.'; $('combined-status').textContent = 'UNAVAILABLE'; $('combined-status').classList.remove('authenticated'); }), 2000);
refresh().catch(() => { $('combined-reason').textContent = 'Could not connect to Clasp.'; });
