const $ = (selector) => document.querySelector(selector);
const state = { eventCount: 0, auditIds: new Set(), stream: null };
const fmtTime = (iso) => new Date(iso).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
const toast = (message) => { const el = $('#toast'); el.textContent = message; el.classList.add('show'); setTimeout(() => el.classList.remove('show'), 2600); };
const escapeText = (text) => String(text ?? '');

function applyIdentity(identity) {
  const pill = $('#state-pill');
  pill.className = `state-pill ${identity.state.toLowerCase()}`;
  $('#identity-state').textContent = identity.state;
  $('#provider-name').textContent = identity.provider ?? 'No active session';
  $('#expires').textContent = identity.expiresAt ? new Date(identity.expiresAt).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' }) : '—';
  $('#identity-visual').classList.toggle('is-authenticated', identity.state === 'AUTHENTICATED');
  $('#core-icon').textContent = identity.state === 'AUTHENTICATED' ? '✓' : '⌑';
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
  if (event.type.startsWith('identity.')) return d.reason ?? event.type.replace('identity.', 'Identity ');
  if (event.type === 'tool.execution.completed') return `${d.tool ?? 'tool'} · ${d.reason ?? d.paymentIntent?.status ?? (d.ok ? 'completed' : 'blocked')}`;
  if (event.type === 'agent.request.received') return 'Agent request received';
  if (event.type === 'agent.response') return 'Agent response ready';
  if (event.type === 'agent.error') return d.message ?? 'Agent request failed';
  return event.type;
}

function eventStatus(event) {
  const d = event.data ?? {};
  if (event.type === 'audit.entry') return d.decision === 'ALLOW' ? (d.executionOutcome === 'succeeded' ? 'SUCCEEDED' : 'ALLOWED') : 'BLOCKED';
  if (event.type === 'authorization.decision') return d.allowed ? 'ALLOWED' : 'DENIED';
  if (event.type === 'identity.authenticated') return 'AUTHENTICATED';
  if (event.type === 'identity.revoked') return 'REVOKED';
  if (event.type === 'identity.expired') return 'EXPIRED';
  if (event.type === 'agent.tool.requested' || event.type === 'agent.tool.proposed') return 'REQUESTED';
  if (event.type === 'tool.execution.completed') return d.decision === 'DENY' ? 'BLOCKED' : (d.ok ? (d.paymentIntent?.status ?? 'DONE') : 'NOT RUN');
  if (event.type === 'agent.error') return 'ERROR';
  return 'LOGGED';
}

function eventIcon(event) {
  if (event.type.startsWith('identity.')) return '◉';
  if (event.type.includes('authorization') || event.type === 'audit.entry') return '◇';
  if (event.type.includes('tool') || event.type.includes('agent')) return '✳';
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
  $('#agent-mode').textContent = data.agent.mode === 'llm' ? `Mastra · ${data.agent.model}` : 'Mastra · local demo parser';
  $('#payment-limit').textContent = `$${(Number(data.stripe.configured ? 5 : 5)).toFixed(2)} · USD · demo merchant`;
  $('#today-date').textContent = new Date().toLocaleDateString([], { weekday: 'long', month: 'long', day: 'numeric', year: 'numeric' }).toUpperCase();
  for (const event of data.events ?? []) {
    addEvent(event, false);
    if (event.type === 'tool.execution.completed') updatePaymentResult(event);
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
    for (const event of snapshot.recent ?? []) addEvent(event, false);
  });
  state.stream.addEventListener('clasp', (message) => addEvent(JSON.parse(message.data)));
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

async function sendRequest(text) {
  if (!text.trim()) return;
  chatBubble(text, 'user');
  const pending = chatBubble('Checking request…', 'agent pending');
  try {
    const result = await post('/api/agent/message', { text, requestId: crypto.randomUUID() });
    pending.textContent = result.answer;
    pending.classList.remove('pending');
  } catch (error) {
    pending.textContent = `Agent error: ${error.message}`;
    pending.classList.remove('pending');
  }
}
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
