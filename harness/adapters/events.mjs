import { createHash } from 'node:crypto';
import { AdapterError, stamp } from './transport.mjs';

export function canonical(value) {
  if (Array.isArray(value)) return value.map(canonical);
  if (value && typeof value === 'object') return Object.fromEntries(Object.keys(value).sort()
    .filter(key => value[key] !== undefined).map(key => [key, canonical(value[key])]));
  return value;
}
export const fingerprint = value => createHash('sha256').update(JSON.stringify(canonical(value)) ?? 'undefined').digest('hex');
export const terminalStatus = status => ['completed', 'interrupted', 'failed'].includes(status);
export const turnKey = (threadId, turnId) => `${threadId}/${turnId}`;

// Projection is intentionally finite; raw tokens/tool payloads never enter diagnostic logs.
export function normalizeNotification(message, hostId, runtimeId, maxText = 262144) {
  const p = message.params ?? {}; const threadId = p.threadId ?? p.thread?.id;
  if (typeof threadId !== 'string') return null;
  const turnId = p.turn?.id ?? p.turnId;
  const common = { formatVersion: '1.0', adapter: 'codex.app-server.stdio', hostId, runtimeId,
    observedAt: stamp(), nativeMethod: message.method, threadId, turnId: turnId ?? null,
    source: 'public_app_server_notification' };
  let fact;
  if (message.method === 'turn/started' && typeof turnId === 'string')
    fact = { ...common, kind: 'turn_started', nativeStatus: p.turn.status, startedAt: p.turn.startedAt ?? null };
  else if (message.method === 'turn/completed' && typeof turnId === 'string')
    fact = { ...common, kind: 'turn_terminal', nativeStatus: p.turn.status,
      completedAt: p.turn.completedAt ?? null, error: p.turn.error ? { message: String(p.turn.error.message ?? '').slice(0, 1024) } : null };
  else if (message.method === 'thread/status/changed')
    fact = { ...common, kind: 'thread_status', nativeStatus: p.status };
  else if (message.method === 'thread/tokenUsage/updated')
    fact = { ...common, kind: 'token_usage', tokenUsage: p.tokenUsage };
  else if (message.method === 'item/completed' && typeof turnId === 'string') {
    fact = { ...common, kind: 'item_completed', itemId: p.item?.id, itemType: p.item?.type };
    if (p.item?.type === 'agentMessage' && typeof p.item.text === 'string') {
      fact.text = p.item.text.slice(0, maxText); fact.textSha256 = fingerprint(p.item.text);
      fact.textTruncated = p.item.text.length > maxText;
      fact.phase = ['commentary', 'partial_answer', 'final_answer'].includes(p.item.phase) ? p.item.phase : null;
    }
    if (p.item?.type === 'userMessage') fact.clientUserMessageId = p.item.clientId ?? null;
  } else return null;
  // Runtime/observation time are excluded so a durable consumer can dedupe replay.
  const { runtimeId: _runtime, observedAt: _time, ...identity } = fact;
  fact.eventId = 'native:' + fingerprint(identity);
  return fact;
}

export class TurnLedger {
  constructor({ maxTurns = 512, maxSeen = 4096 } = {}) {
    this.turns = new Map(); this.seen = new Set(); this.maxTurns = maxTurns; this.maxSeen = maxSeen;
  }
  ensure(threadId, turnId) {
    const key = turnKey(threadId, turnId);
    if (!this.turns.has(key)) {
      if (this.turns.size >= this.maxTurns) {
        const removable = [...this.turns].find(([, value]) => terminalStatus(value.status) && !value.owned);
        if (removable) this.turns.delete(removable[0]);
        else throw new AdapterError('CAPACITY', 'Turn tracking bound reached; persist and rotate an idle runtime');
      }
      this.turns.set(key, { threadId, turnId, status: 'UNKNOWN', owned: false,
        acknowledged: false, started: false, terminal: false, conflict: false });
    }
    return this.turns.get(key);
  }
  acknowledge(threadId, turnId, facts = {}) {
    const turn = this.ensure(threadId, turnId);
    Object.assign(turn, facts, { acknowledged: true, owned: true });
    // An initial inProgress response is acknowledgement, not a started notification.
    return { ...turn };
  }
  accept(fact) {
    if (this.seen.has(fact.eventId)) return { duplicate: true };
    this.seen.add(fact.eventId);
    if (this.seen.size > this.maxSeen) this.seen.delete(this.seen.values().next().value);
    if (!fact.turnId || !['turn_started', 'turn_terminal', 'turn_reconciled'].includes(fact.kind)) return { duplicate: false };
    const turn = this.ensure(fact.threadId, fact.turnId);
    turn.lastObservedAt = fact.observedAt;
    if (fact.kind === 'turn_started') {
      turn.started = true; turn.startedObservation = fact.eventId;
      if (!turn.terminal && !turn.conflict) turn.status = 'inProgress';
    } else if (terminalStatus(fact.nativeStatus)) {
      if (turn.terminal && turn.status !== fact.nativeStatus) { turn.conflict = true; turn.status = 'UNKNOWN'; }
      else if (!turn.conflict) { turn.status = fact.nativeStatus; turn.terminal = true; turn.terminalObservation = fact.eventId; }
    } else if (fact.kind === 'turn_reconciled' && fact.nativeStatus === 'inProgress' && !turn.terminal && !turn.conflict)
      turn.status = 'inProgress'; // readback does not synthesize turn/started.
    else if (fact.kind === 'turn_terminal') { turn.conflict = true; turn.status = 'UNKNOWN'; }
    return { duplicate: false, turn: { ...turn } };
  }
  get(threadId, turnId) { const result = this.turns.get(turnKey(threadId, turnId)); return result ? { ...result } : null; }
}

// Waits are bounded, cached and cancellable. Disconnect wakes immediately.
export function waitForLedger(adapter, threadId, turnId, predicate, timeoutMs = 20000) {
  return new Promise((resolve, reject) => {
    let timer, done = false;
    const cleanup = () => { clearTimeout(timer); adapter.off('turn', check); adapter.off('disconnect', disconnected); adapter.off('closing', disconnected); };
    const check = () => { const turn = adapter.ledger.get(threadId, turnId); if (!done && turn && predicate(turn)) { done = true; cleanup(); resolve(turn); } };
    const disconnected = () => { if (done) return; done = true; cleanup(); reject(new AdapterError('UNKNOWN', 'Runtime disconnected; use exact read-only reconciliation', { threadId, turnId, noAutomaticRetry: true })); };
    timer = setTimeout(() => { if (done) return; done = true; cleanup(); reject(new AdapterError('UNKNOWN', 'Matching native event not observed before timeout', { threadId, turnId, noAutomaticRetry: true })); }, timeoutMs);
    adapter.on('turn', check); adapter.on('disconnect', disconnected); adapter.on('closing', disconnected);
    check(); if (!adapter.connected) disconnected();
  });
}
