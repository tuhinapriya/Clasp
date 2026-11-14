import { randomUUID } from 'node:crypto';
import { EventEmitter } from 'node:events';

export type ClaspEvent = {
  id: string;
  type: string;
  at: string;
  data: Record<string, unknown>;
};

const emitter = new EventEmitter();
emitter.setMaxListeners(100);
const history: ClaspEvent[] = [];

export function publish(type: string, data: Record<string, unknown> = {}) {
  const event = { id: randomUUID(), type, at: new Date().toISOString(), data };
  history.push(event);
  if (history.length > 250) history.shift();
  emitter.emit('event', event);
  return event;
}

export function recentEvents(limit = 80) {
  return history.slice(-limit);
}

export function subscribe(listener: (event: ClaspEvent) => void) {
  emitter.on('event', listener);
  return () => emitter.off('event', listener);
}
