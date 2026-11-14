export function createVexoController({ onMessage = () => {}, onReady = () => {} } = {}) {
  onReady({ available: true });

  async function post(path, body = {}) {
    const response = await fetch(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    const result = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(result.error ?? `Clasp Vexo bridge returned ${response.status}.`);
    return result;
  }

  async function connect() {
    onMessage('connecting', 'Connecting only to the configured Vexo band through the local Vexo Open Kit SDK. Double-tap the band if needed.');
    const result = await post('/api/hardware/vexo/connect');
    onMessage('connecting', result.message ?? 'Waiting for the Vexo band…');
    return result;
  }

  async function disconnect() {
    return post('/api/hardware/vexo/disconnect');
  }

  async function startMicrophone() {
    onMessage('microphone-pending', 'Starting the real Vexo microphone. The band blue light will turn on.');
    return post('/api/hardware/vexo/microphone', { active: true });
  }

  async function stopMicrophone() {
    return post('/api/hardware/vexo/microphone', { active: false });
  }

  return { connect, disconnect, startMicrophone, stopMicrophone };
}
