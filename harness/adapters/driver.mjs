/** Bounded one-shot wiring. State owns authorization, intents, controls and facts. */
import path from 'node:path';
import { readFile, writeFile, open, mkdir, rename, stat } from 'node:fs/promises';
import { createHash } from 'node:crypto';
import { CodexAdapter } from './codex.mjs';
import { AdapterError, StdioTransport, stamp } from './transport.mjs';
import { fingerprint } from './events.mjs';
import { stateCallbacks } from '../runtime_bridge.mjs';

export const DRIVER_VERSION = '1.0.1';
const sha = value => createHash('sha256').update(value).digest('hex');
const clone = value => JSON.parse(JSON.stringify(value));
const fail = (code, message, facts = {}) => { throw new AdapterError(code, message, facts); };
const exactId = (value, label) => {
  if (typeof value !== 'string' || !/^[A-Za-z0-9_-]{1,100}$/.test(value)) fail('INVALID_MANIFEST', `Exact ${label} required`);
  return value;
};
const boundedString = (value, label, max = 2048) => {
  if (typeof value !== 'string' || !value.trim() || Buffer.byteLength(value) > max) fail('INVALID_MANIFEST', `Bounded ${label} required`);
  return value;
};
const absolute = (value, label, interpreterPath = false) => {
  boundedString(value, label);
  if (!(path.isAbsolute(value) || (interpreterPath && (path.posix.isAbsolute(value) || path.win32.isAbsolute(value)))))
    fail('INVALID_MANIFEST', `${label} must be absolute in its explicit interpreter namespace`);
  return value;
};
function integer(value, min, max, label) {
  if (!Number.isInteger(value) || value < min || value > max) fail('INVALID_MANIFEST', `${label} must be ${min}..${max}`);
  return value;
}
function only(object, keys, label) {
  if (!object || typeof object !== 'object' || Array.isArray(object) || Object.keys(object).some(key => !keys.includes(key)))
    fail('INVALID_MANIFEST', `${label} contains unsupported fields`);
}
const errorFact = error => ({ code: error.code ?? 'DRIVER_FAILED', message: String(error.message ?? error).slice(0, 1024),
  ...(typeof error.facts?.method === 'string' ? { method: error.facts.method.slice(0, 100) } : {}),
  ...(Number.isInteger(error.facts?.rpcCode) ? { rpcCode: error.facts.rpcCode } : {}),
  ...(Number.isInteger(error.facts?.requestId) ? { requestId: error.facts.requestId } : {}),
  ...(error.facts?.threadId ? { target: { threadId: error.facts.threadId } } : {}),
  ...(error.facts?.mayHaveBeenSent !== undefined ? { mayHaveBeenSent: error.facts.mayHaveBeenSent } : {}), noAutomaticRetry: true });

export function validateManifest(value) {
  const m = clone(value);
  only(m, ['formatVersion', 'runId', 'source', 'artifactDir', 'state', 'native', 'owner', 'workId', 'taskId',
    'route', 'settings', 'turn', 'session', 'policyId', 'resources', 'bounds', 'timeline', 'historyAnchor', 'controls'], 'manifest');
  if (m.formatVersion !== '1.0') fail('INVALID_MANIFEST', 'Unsupported driver manifest format');
  for (const key of ['runId', 'workId', 'taskId', 'policyId']) exactId(m[key], key);
  boundedString(m.source, 'source'); absolute(m.artifactDir, 'artifactDir');
  only(m.state, ['command', 'args', 'root', 'store', 'timeoutMs', 'maxBytes'], 'state');
  boundedString(m.state.command, 'State interpreter'); absolute(m.state.root, 'State root', true); absolute(m.state.store, 'State store', true);
  if (!Array.isArray(m.state.args) || !m.state.args.length || m.state.args.length > 16 || m.state.args.some(x => typeof x !== 'string' || Buffer.byteLength(x) > 2048))
    fail('INVALID_MANIFEST', 'Explicit bounded State CLI argv required');
  integer(m.state.timeoutMs, 100, 10000, 'State timeoutMs'); integer(m.state.maxBytes, 4096, 8 * 1024 * 1024, 'State maxBytes');
  only(m.native, ['hostId', 'command', 'processCwd', 'allowedCwds', 'disabledFeatures', 'requestTimeoutMs'], 'native');
  exactId(m.native.hostId, 'hostId'); boundedString(m.native.command, 'Codex command'); absolute(m.native.processCwd, 'native processCwd');
  if (!Array.isArray(m.native.allowedCwds) || m.native.allowedCwds.length !== 1) fail('INVALID_MANIFEST', 'One exact allowed cwd required');
  absolute(m.native.allowedCwds[0], 'allowed cwd');
  if (!Array.isArray(m.native.disabledFeatures) || m.native.disabledFeatures.length > 64 || m.native.disabledFeatures.some(x => !/^[a-z0-9_]+$/.test(x)))
    fail('INVALID_MANIFEST', 'Explicit disabled feature names required; no strong isolation inferred');
  integer(m.native.requestTimeoutMs, 100, 10000, 'native requestTimeoutMs');
  only(m.owner, ['id', 'revision'], 'owner'); exactId(m.owner.id, 'coordinator'); integer(m.owner.revision, 1, 1000000, 'coordinator revision');
  only(m.route, ['mode', 'expectedRevision', 'reviewOwner', 'threadId', 'priorOwnership'], 'route');
  if (!['create', 'resume'].includes(m.route.mode)) fail('INVALID_MANIFEST', 'Route mode must be create or closed-owned resume');
  integer(m.route.expectedRevision, 0, 1000000, 'route expectedRevision'); exactId(m.route.reviewOwner, 'reviewOwner');
  if (m.route.mode === 'create' && (m.route.threadId !== null || m.route.priorOwnership)) fail('INVALID_MANIFEST', 'Create cannot name or take over an existing thread');
  if (m.route.mode === 'resume') {
    exactId(m.route.threadId, 'resume threadId');
    const prior = m.route.priorOwnership;
    if (!prior || prior.hostId !== m.native.hostId || prior.threadId !== m.route.threadId || !prior.closedFactId || !prior.runtimeId || !prior.terminalTurnId)
      fail('INVALID_MANIFEST', 'Resume requires exact prior owned terminal/closure references; State verifies them');
  }
  only(m.settings, ['model', 'reasoningEffort', 'cwd', 'sandboxMode', 'sandboxPolicy', 'approvalPolicy'], 'settings');
  boundedString(m.settings.model, 'model', 100); boundedString(m.settings.reasoningEffort, 'thinking', 100);
  if (m.settings.cwd !== m.native.allowedCwds[0]) fail('INVALID_MANIFEST', 'Settings cwd must equal the exact allowed cwd');
  if (m.settings.approvalPolicy === undefined || !m.settings.sandboxPolicy || !m.settings.sandboxMode) fail('INVALID_MANIFEST', 'Explicit native permissions required');
  only(m.turn, ['input', 'clientUserMessageId'], 'turn'); exactId(m.turn.clientUserMessageId, 'clientUserMessageId');
  if (!Array.isArray(m.turn.input) || !m.turn.input.length || m.turn.input.length > 16 ||
      m.turn.input.some(item => !item || item.type !== 'text' || typeof item.text !== 'string' || Object.keys(item).some(k => !['type', 'text'].includes(k))) ||
      Buffer.byteLength(JSON.stringify(m.turn.input)) > 32768) fail('INVALID_MANIFEST', 'At most 32KiB explicit text input required');
  only(m.session, ['reason', 'ephemeral', 'baseInstructions', 'developerInstructions'], 'session'); boundedString(m.session.reason, 'session choice reason');
  if (m.session.ephemeral !== false) fail('INVALID_MANIFEST', 'This reusable driver requires explicit ephemeral:false');
  for (const key of ['baseInstructions', 'developerInstructions']) if (m.session[key] !== undefined && (typeof m.session[key] !== 'string' || Buffer.byteLength(m.session[key]) > 16384)) fail('INVALID_MANIFEST', 'Instructions exceed driver bound');
  if (!Array.isArray(m.resources) || m.resources.length > 32 || m.resources.some(x => typeof x !== 'string' || !x || x.length > 160)) fail('INVALID_MANIFEST', 'Explicit bounded resource IDs required');
  only(m.bounds, ['walltimeMs', 'shutdownGraceMs', 'controlPollMs', 'timelineIntervalMs'], 'bounds');
  integer(m.bounds.walltimeMs, 100, 86400000, 'walltimeMs'); integer(m.bounds.shutdownGraceMs, 100, 600000, 'shutdownGraceMs');
  integer(m.bounds.controlPollMs, 25, 900000, 'controlPollMs'); integer(m.bounds.timelineIntervalMs, 25, 3600000, 'timelineIntervalMs');
  if (m.bounds.controlPollMs > Math.min(m.bounds.walltimeMs, m.bounds.shutdownGraceMs)) fail('INVALID_MANIFEST', 'Control poll must fit within both frozen deadlines');
  only(m.timeline, ['maxPages', 'pageSize', 'maxItemPages', 'itemPageSize', 'maxTurns', 'maxItems', 'maxMessages', 'maxTextBytes', 'maxTotalTextBytes', 'maxRequests', 'deadlineMs'], 'timeline');
  integer(m.timeline.deadlineMs, 100, 10000, 'timeline deadlineMs');
  for (const [key, low, high] of [['maxPages', 1, 10], ['pageSize', 1, 50], ['maxItemPages', 1, 10], ['itemPageSize', 1, 100],
    ['maxTurns', 1, 100], ['maxItems', 1, 1000], ['maxMessages', 1, 128], ['maxTextBytes', 0, 65536], ['maxTotalTextBytes', 0, 262144], ['maxRequests', 1, 100]])
    if (m.timeline[key] !== undefined) integer(m.timeline[key], low, high, 'timeline ' + key);
  if (m.historyAnchor !== null && m.historyAnchor !== undefined) {
    only(m.historyAnchor, ['actionId', 'turnId'], 'historyAnchor'); exactId(m.historyAnchor.actionId, 'anchor actionId'); exactId(m.historyAnchor.turnId, 'anchor turnId');
    if (m.route.mode !== 'resume') fail('INVALID_MANIFEST', 'New thread cannot supply a history anchor');
  }
  if (!Array.isArray(m.controls) || m.controls.length > 4) fail('INVALID_MANIFEST', 'At most four frozen exact control slots required');
  const controlIds = new Set();
  for (const slot of m.controls) {
    only(slot, ['id', 'kind', 'trigger'], 'control slot'); exactId(slot.id, 'control ID');
    if (controlIds.has(slot.id) || !['pause', 'correct'].includes(slot.kind) || !['external', 'deadline'].includes(slot.trigger) ||
        (slot.trigger === 'deadline' && slot.kind !== 'pause')) fail('INVALID_MANIFEST', 'Control slots must be unique explicit pause/correct; deadline only pauses');
    controlIds.add(slot.id);
  }
  if (m.controls.filter(x => x.trigger === 'deadline').length > 1) fail('INVALID_MANIFEST', 'At most one deadline pause');
  return m;
}

export async function loadManifest(filename, expectedSha256) {
  if (!/^[a-f0-9]{64}$/.test(expectedSha256 ?? '')) fail('MANIFEST_HASH_REQUIRED', 'Use the independently frozen exact manifest SHA-256');
  const info = await stat(filename); if (info.size > 131072) fail('INVALID_MANIFEST', 'Manifest exceeds 128KiB');
  const raw = await readFile(filename); const digest = sha(raw);
  if (digest !== expectedSha256) fail('MANIFEST_HASH_MISMATCH', 'Manifest changed; review its exact content before running');
  return { manifest: validateManifest(JSON.parse(raw.toString('utf8'))), manifestSha256: digest };
}

/** These are exact public request builders, not policy checks. State hashes the same params. */
export function nativeParameters(manifest, method, threadId, turnId, correction) {
  const s = manifest.settings;
  if (method === 'thread/start') return { model: s.model, cwd: s.cwd, sandbox: s.sandboxMode, approvalPolicy: s.approvalPolicy,
    ephemeral: manifest.session.ephemeral, config: { model_reasoning_effort: s.reasoningEffort },
    ...(manifest.session.baseInstructions !== undefined ? { baseInstructions: manifest.session.baseInstructions } : {}),
    ...(manifest.session.developerInstructions !== undefined ? { developerInstructions: manifest.session.developerInstructions } : {}) };
  if (method === 'thread/resume') return { threadId, excludeTurns: true, model: s.model, cwd: s.cwd, sandbox: s.sandboxMode,
    approvalPolicy: s.approvalPolicy, config: { model_reasoning_effort: s.reasoningEffort } };
  if (method === 'turn/start') return { threadId, model: s.model, effort: s.reasoningEffort, cwd: s.cwd,
    sandboxPolicy: s.sandboxPolicy, approvalPolicy: s.approvalPolicy, ...clone(manifest.turn) };
  if (method === 'turn/interrupt') return { threadId, turnId };
  if (method === 'turn/steer') return { threadId, expectedTurnId: turnId, input: clone(correction.input), clientUserMessageId: correction.clientUserMessageId };
  fail('UNSUPPORTED', 'Unsupported driver native operation');
}

async function boundedJson(filename, max = 262144) {
  const info = await stat(filename); if (info.size > max) fail('ARTIFACT_BOUND', 'Driver artifact exceeds declared read bound');
  return JSON.parse(await readFile(filename, 'utf8'));
}
async function exclusiveJson(filename, value) {
  const fd = await open(filename, 'wx');
  try { await fd.writeFile(JSON.stringify(value, null, 2) + '\n'); await fd.sync(); } finally { await fd.close(); }
}
async function snapshot(dir, value) {
  const filename = path.join(dir, 'status.json'); const pending = path.join(dir, 'status.next.json');
  await writeFile(pending, JSON.stringify(value, null, 2) + '\n'); await rename(pending, filename);
}

export class OwnedStdioDriver {
  constructor({ manifest, manifestSha256, callbacks, transport, versionEvidence, adapterFactory, now = Date.now } = {}) {
    this.manifest = validateManifest(manifest); this.manifestSha256 = manifestSha256;
    if (!/^[a-f0-9]{64}$/.test(manifestSha256 ?? '')) fail('MANIFEST_HASH_REQUIRED', 'Frozen manifest digest required');
    this.callbacks = callbacks ?? stateCallbacks(this.manifest.state); this.now = now;
    const m = this.manifest;
    this.adapter = (adapterFactory ?? (options => new CodexAdapter(options)))({ hostId: m.native.hostId,
      command: m.native.command, processCwd: m.native.processCwd, allowedCwds: m.native.allowedCwds,
      allowedThreadIds: m.route.threadId ? [m.route.threadId] : [], versionEvidence,
      transport: transport ?? new StdioTransport({ command: m.native.command, cwd: m.native.processCwd,
        disabledFeatures: m.native.disabledFeatures, requestTimeoutMs: m.native.requestTimeoutMs }),
      factSink: fact => this.callbacks.factSink(fact),
      guard: async request => {
        // A fresh metadata bootstrap only serves this runtime's one first-start intent.
        if (request.method === 'turn/start' && request.intentId === this.bootstrapIntentId) {
          if (this.bootstrapGuardUsed) fail('BOOTSTRAP_USED', 'First-turn bootstrap guard is single-use');
          this.bootstrapGuardUsed = true;
          request.bootstrapProof = await this.bootstrapObservation('before_actual_guard');
        } else if (request.threadId) await this.scan('before_actual_guard', request.threadId, request.method === 'turn/interrupt');
        this.withinDeadline(request.method);
        const receipt = await this.callbacks.guard(request);
        if (request.bootstrapProof && receipt?.newOwnedThreadBootstrapVerified !== true)
          fail('GUARD_DENIED', 'Core did not verify this exact first-turn bootstrap proof', { mayHaveBeenSent: false });
        return receipt;
      } });
    this.state = { formatVersion: '1.0', driverVersion: DRIVER_VERSION, runId: m.runId, manifestSha256,
      hostId: m.native.hostId, runtimeId: this.adapter.runtimeId, workId: m.workId, taskId: m.taskId,
      threadId: m.route.threadId, turnId: null, phase: 'constructed', actions: [], controls: [],
      scientificAdoption: 'not_performed', uiRouteVerified: false, isolation: 'not_claimed',
      controllerCoverage: 'Explicit control slots only; timeline does not decode all controller/delegation/tool wrappers' };
    this.anchor = null; this.processedControls = new Set(); this.logBytes = 0; this.logCount = 0; this.started = false;
    this.adapter.on('fact', fact => {
      if (fact.kind === 'thread_timeline_observed') this.lastTimelineFact = clone(fact);
      if (fact.threadId === this.state.threadId && (fact.turnId || fact.kind === 'native_user_message_observed' ||
          (fact.kind === 'thread_read' && ['active', 'systemError'].includes(fact.thread?.status?.type)) ||
          (fact.kind === 'thread_status' && fact.nativeStatus?.type !== 'idle'))) this.bootstrapActivitySeen = true;
    });
  }
  async log(kind, facts = {}) {
    const row = { kind, at: stamp(), ...facts }; const data = JSON.stringify(row) + '\n';
    if (this.logCount >= 256 || this.logBytes + Buffer.byteLength(data) > 131072) {
      this.state.logCoverage = { truncated: true, retainedEvents: this.logCount, retainedBytes: this.logBytes,
        durableFacts: 'Full native facts remain in the same State; this file is bounded diagnostics only' }; return;
    }
    this.logCount++; this.logBytes += Buffer.byteLength(data);
    const fd = await open(path.join(this.manifest.artifactDir, 'events.jsonl'), 'a');
    try { await fd.writeFile(data); } finally { await fd.close(); }
  }
  async save() { this.state.updatedAt = stamp(); await snapshot(this.manifest.artifactDir, this.state); }
  withinDeadline(method) {
    const m = this.manifest, limit = m.bounds.walltimeMs + (['turn/interrupt', 'turn/steer'].includes(method) ? m.bounds.shutdownGraceMs : 0);
    if (this.now() - this.beganAt >= limit) fail('RUNNER_DEADLINE', 'Frozen deadline reached before another native action', { mayHaveBeenSent: false });
  }
  async taskView() {
    const data = await this.callbacks.call('runtime-overview', { args: ['--work', this.manifest.workId, '--limit', '100'] });
    const matches = data.tasks?.filter(item => item.id === this.manifest.taskId && item.work_id === this.manifest.workId) ?? [];
    if (matches.length !== 1) fail('OUT_OF_SCOPE', 'Exact task not visible within bounded work overview; no action issued');
    return matches[0];
  }
  async scan(reason, threadId = this.state.threadId, allowIncomplete = false) {
    if (!threadId) fail('IDENTITY_MISMATCH', 'Timeline requires the exact mapped thread');
    let observed;
    try { observed = await this.adapter.timeline({ threadId, anchor: this.anchor, ...this.manifest.timeline }); }
    catch (error) {
      // A fatal identity read error can still have a durably recorded exact-scope gap.
      // Preserve that real scan for interruption/terminal reporting, never invent complete.
      const fact = this.lastTimelineFact;
      if (!allowIncomplete || !fact || fact.threadId !== threadId || fact.eventId !== error.facts?.scanEventId) throw error;
      await this.adapter.flush();
      observed = { scan: fact, coverage: fact.coverage, latestObservedTurnId: fact.latestObservedTurnId,
        observedAt: fact.observedAt, factsPersisted: !!this.adapter.factSink && !this.adapter.persistenceError };
    }
    this.state.lastTimeline = { reason, eventId: observed.scan.eventId, observedAt: observed.observedAt,
      anchor: clone(this.anchor), latestObservedTurnId: observed.latestObservedTurnId, coverage: observed.coverage,
      factsPersisted: observed.factsPersisted };
    await this.log('timeline', { reason, eventId: observed.scan.eventId, complete: observed.coverage.complete,
      gap: observed.coverage.gap, messageEventIds: observed.scan.messageEventIds });
    await this.save();
    if (!observed.factsPersisted) fail('PERSISTENCE_FAILED', 'Exact-thread scan did not commit; native writes stopped', { threadId, mayHaveBeenSent: false });
    this.lastScanAt = this.now();
    if (!allowIncomplete && observed.coverage.complete !== true) fail('TIMELINE_INCOMPLETE', 'Fresh exact-thread history was incomplete; new work/correction stopped',
      { threadId, scanEventId: observed.scan.eventId, mayHaveBeenSent: false });
    return observed;
  }
  async verifiedAnchor(actionId, turnId) {
    const action = await this.callbacks.call('runtime-action', { args: ['--id', actionId] });
    if (action.host_id !== this.state.hostId || action.thread_id !== this.state.threadId || action.turn_id !== turnId ||
        action.method !== 'turn/start' || !action.body?.ack_event || !action.body?.attempt_event)
      fail('ANCHOR_UNVERIFIED', 'History anchor must match an actual persisted acknowledged start on this exact host/thread/turn');
    this.anchor = { turnId };
    this.state.anchorProof = { actionId, turnId, ackEventId: action.body.ack_event, attemptEventId: action.body.attempt_event };
  }
  async bootstrapObservation(reason) {
    const m = this.manifest, threadId = this.state.threadId;
    if (m.route.mode !== 'create' || !threadId || !this.adapter.connected || this.adapter.closed ||
        this.bootstrapActivitySeen || this.adapter.unknownThreads.has(threadId) ||
        [...this.adapter.ledger.turns.values()].some(turn => turn.threadId === threadId) ||
        [...this.adapter.actions.values()].some(action => action.threadId === threadId && action.method.startsWith('turn/')))
      fail('BOOTSTRAP_UNAVAILABLE', 'Bootstrap only applies to this live runtime before any native turn attempt/activity', { mayHaveBeenSent: false });
    const binding = this.adapter.ownedThread(threadId), sessionId = this.manifest.runId + '-session';
    const session = await this.callbacks.call('runtime-action', { args: ['--id', sessionId] });
    if (session.method !== 'thread/start' || session.task_id !== m.taskId || session.host_id !== this.state.hostId ||
        session.thread_id !== threadId || session.runtime_id !== this.state.runtimeId || session.body?.phase !== 'identity_acknowledged' ||
        !session.body.ack_event || !session.body.attempt_event || binding.ownershipIntentId !== sessionId)
      fail('BOOTSTRAP_UNAVAILABLE', 'Fresh thread needs its actual same-runtime durable creation ACK', { mayHaveBeenSent: false });
    let metadata;
    try { metadata = await this.adapter.readThread(threadId); await this.adapter.flush(); }
    catch (error) {
      if (error instanceof AdapterError) error.facts = { ...error.facts, threadId };
      throw error;
    }
    if (metadata.thread.status?.type !== 'idle' || this.bootstrapActivitySeen)
      fail('BOOTSTRAP_UNAVAILABLE', 'Fresh metadata must report exact loaded idle; notLoaded/active/error are not empty history', { mayHaveBeenSent: false });
    const proof = { mode: 'new_owned_thread_pre_first_turn', session_action_id: sessionId,
      session_ack_event_id: session.body.ack_event, metadata_event_id: metadata.eventId,
      host_id: this.state.hostId, runtime_id: this.state.runtimeId, thread_id: threadId };
    this.state.lastBootstrapObservation = { reason, proof, observedAt: metadata.observedAt,
      state: 'NEW_THREAD_BOOTSTRAP', historyComplete: false, historyReadPerformed: false,
      coverage: 'Same live owned creation ACK plus loaded-idle metadata before first turn; non-atomic input observation' };
    await this.log('new_thread_bootstrap', { reason, proof, historyComplete: false }); await this.save();
    return proof;
  }
  async prepare(actionId, method, params, extra = {}) {
    let bootstrapProof;
    const first = method === 'turn/start' && this.manifest.route.mode === 'create' && !this.state.startActionId &&
      ![...this.adapter.actions.values()].some(action => action.threadId === params.threadId && action.method.startsWith('turn/'));
    if (first) {
      if (this.bootstrapSelectionUsed || this.bootstrapIntentId) fail('BOOTSTRAP_USED', 'A first-turn selection/intent already closed bootstrap; inspect it rather than preparing another');
      this.bootstrapSelectionUsed = true; this.state.bootstrapSelectionId = actionId; await this.save();
      bootstrapProof = await this.bootstrapObservation('before_prepare');
      // Mark before the durable call: timeout or failed guard never reopens bootstrap.
      this.bootstrapIntentId = actionId; this.state.bootstrapIntentId = actionId; await this.save();
    } else if (params.threadId) await this.scan('before_prepare', params.threadId, method === 'turn/interrupt');
    this.withinDeadline(method);
    const view = await this.taskView(), route = view.route;
    if (!route || route.host_id !== this.state.hostId || route.runtime_id !== this.state.runtimeId || route.thread_id !== (params.threadId ?? null))
      fail('IDENTITY_MISMATCH', 'Current route no longer matches this owned runtime; no takeover or resend');
    const m = this.manifest;
    const intent = await this.callbacks.call('runtime-prepare', { args: ['--id', actionId, '--task', m.taskId,
      '--expected', String(route.revision), '--owner', m.owner.id, '--owner-expected', String(m.owner.revision), '--source', m.source],
      body: { method, params, policy_id: m.policyId, resources: m.resources,
        ...(bootstrapProof ? { bootstrap_proof: bootstrapProof } : {}),
        ...(route.body?.recovery_context && !route.body.recovery_context.consumed_by
          ? { recovery_context_sha256: route.body.recovery_context.context_sha256 } : {}), ...extra } });
    this.state.actions.push({ actionId, method, requestSha256: fingerprint(params) });
    await this.log('intent_prepared', { actionId, method, requestSha256: intent.requestSha256 }); await this.save(); return intent;
  }
  async initializeRoute() {
    const m = this.manifest, view = await this.taskView(), old = view.route;
    if ((old?.revision ?? 0) !== m.route.expectedRevision || (old && old.thread_id !== m.route.threadId))
      fail('REVISION_CONFLICT', 'Initial route differs from the frozen manifest');
    await this.callbacks.call('runtime-route', { args: ['--task', m.taskId, '--expected', String(m.route.expectedRevision), '--source', m.source],
      body: { host_id: m.native.hostId, thread_id: m.route.threadId, runtime_id: this.adapter.runtimeId, review_owner: m.route.reviewOwner,
        coordinator: m.owner.id, owner_revision: m.owner.revision, control_mode: 'owned_stdio', capabilities: { interrupt: true, steer: true } } });
    await this.adapter.connect(); await this.adapter.flush();
    if (m.historyAnchor) await this.verifiedAnchor(m.historyAnchor.actionId, m.historyAnchor.turnId);
  }
  async openThread() {
    const m = this.manifest; this.adapter.settings(m.settings); this.adapter.input(m.turn.input);
    const method = m.route.mode === 'create' ? 'thread/start' : 'thread/resume';
    const actionId = m.runId + '-session', params = nativeParameters(m, method, this.state.threadId);
    const intent = await this.prepare(actionId, method, params, { session_choice_reason: m.session.reason,
      ...(m.route.priorOwnership ? { prior_ownership: m.route.priorOwnership } : {}) });
    const response = method === 'thread/start'
      ? await this.adapter.createThread({ settings: m.settings, intent, ...m.session })
      : await this.adapter.resumeThread({ threadId: this.state.threadId, settings: m.settings, intent, priorOwnership: m.route.priorOwnership });
    this.state.threadId = response.threadId; this.state.entry = response.entry;
    this.state.sessionReceipt = response; this.state.phase = 'session_acknowledged'; await this.save();
  }
  async startTurn() {
    const m = this.manifest, params = nativeParameters(m, 'turn/start', this.state.threadId), actionId = m.runId + '-start';
    const intent = await this.prepare(actionId, 'turn/start', params, { message: { threadId: this.state.threadId,
      prompt: m.turn.input.map(item => item.text).join('\n'), model: m.settings.model, thinking: m.settings.reasoningEffort } });
    this.state.startActionId = actionId; await this.save();
    const response = await this.adapter.startTurn({ threadId: this.state.threadId, settings: m.settings, ...m.turn, intent });
    this.state.turnId = response.turnId; this.state.startActionId = actionId;
    this.state.phase = 'turn_acknowledged'; this.state.startReceipt = response;
    await this.verifiedAnchor(actionId, response.turnId); await this.save();
  }
  currentTurn() { return this.state.turnId ? this.adapter.ledger.get(this.state.threadId, this.state.turnId) : null; }
  async submitDeadline(slot) {
    const m = this.manifest;
    // The exact deadline pause is declared in the frozen manifest, not inferred from model text.
    const payload = { reason: 'Frozen one-shot runner walltime reached; pause only its exact mapped task',
      source_ref: { kind: 'frozen_driver_deadline', manifestSha256: this.manifestSha256, runId: m.runId }, origin_status: 'manifest_declared_deadline' };
    await submitControl({ manifest: m, manifestSha256: this.manifestSha256, slotId: slot.id, payload,
      callbacks: this.callbacks, internalDeadline: true });
  }
  async pollControls() {
    const turn = this.currentTurn(); if (!turn?.owned || !turn.started || turn.terminal || turn.conflict) return;
    for (const slot of this.manifest.controls) {
      if (this.processedControls.has(slot.id)) continue;
      this.withinDeadline('turn/interrupt');
      const rows = await this.callbacks.call('runtime-control-view', { args: ['--id', slot.id] });
      if (!Array.isArray(rows) || rows.length === 0) continue;
      this.processedControls.add(slot.id);
      if (rows.length !== 1 || rows[0].task_id !== this.manifest.taskId || rows[0].kind !== slot.kind || rows[0].status !== 'requested' ||
          !rows[0].body?.affected_actions?.includes(this.state.startActionId)) {
        await this.log('control_not_applicable', { controlId: slot.id, status: rows[0]?.status ?? 'invalid' }); continue;
      }
      const row = rows[0], method = slot.kind === 'pause' ? 'turn/interrupt' : 'turn/steer';
      const correction = row.body.driver_correction;
      if (slot.kind === 'correct' && (!correction || correction.expectedTurnId !== this.state.turnId))
        fail('IDENTITY_MISMATCH', 'Correction must name the exact current turn; no automatic next-turn send');
      const actionId = this.manifest.runId + '-control-' + this.manifest.controls.indexOf(slot);
      this.state.controls.push({ controlId: slot.id, actionId, method, source: row.source,
        sourceRef: row.body.source_ref ?? null, inputId: row.body.input_id ?? null, originStatus: row.body.origin_status ?? 'unverified' });
      await this.save();
      try {
        const params = nativeParameters(this.manifest, method, this.state.threadId, this.state.turnId, correction);
        const intent = await this.prepare(actionId, method, params, { control_id: slot.id });
        const receipt = slot.kind === 'pause'
          ? await this.adapter.interrupt({ threadId: this.state.threadId, turnId: this.state.turnId, intent, timeoutMs: this.manifest.native.requestTimeoutMs })
          : await this.adapter.steer({ threadId: this.state.threadId, turnId: this.state.turnId, ...correction, intent, timeoutMs: this.manifest.native.requestTimeoutMs });
        this.state.controls.at(-1).receipt = receipt;
        await this.log('control_receipt', { controlId: slot.id, actionId, acknowledged: receipt.acknowledged ?? false, skipped: receipt.skipped ?? null });
      } catch (error) {
        this.state.controls.at(-1).error = errorFact(error); await this.log('control_failed', { controlId: slot.id, error: errorFact(error) });
        // Once prepared/attempted, this slot never retries. Exact readonly recovery remains available.
        if (['TIMELINE_INCOMPLETE', 'PERSISTENCE_FAILED', 'UNKNOWN'].includes(error.code)) throw error;
      }
      await this.save();
    }
  }
  async wait() {
    const m = this.manifest; let deadlineSubmitted = false;
    while (true) {
      const turn = this.currentTurn();
      if (turn?.conflict) fail('UNKNOWN', 'Conflicting owned terminal observations require explicit core reconciliation');
      if (turn?.terminal) { await this.adapter.flush(); await this.scan('terminal_readback', this.state.threadId, true); return turn; }
      if (!this.adapter.connected || this.adapter.persistenceError) fail('UNKNOWN', 'Owned runtime disconnected or persistence failed');
      const elapsed = this.now() - this.beganAt;
      if (elapsed >= m.bounds.walltimeMs && !deadlineSubmitted) {
        deadlineSubmitted = true; this.state.deadlineReachedAt = stamp(); await this.save();
        const slot = m.controls.find(x => x.trigger === 'deadline');
        if (slot) await this.submitDeadline(slot);
        else fail('RUNNER_DEADLINE', 'Frozen one-shot walltime reached; no deadline interrupt authorized');
      }
      if (elapsed >= m.bounds.walltimeMs + m.bounds.shutdownGraceMs) fail('UNKNOWN', 'No matching terminal by the bounded shutdown deadline');
      if (this.now() - (this.lastScanAt ?? 0) >= m.bounds.timelineIntervalMs) await this.scan('while_waiting', this.state.threadId, true);
      await this.pollControls();
      await new Promise(resolve => setTimeout(resolve, m.bounds.controlPollMs));
    }
  }
  async run() {
    if (this.started) fail('RUN_ALREADY_USED', 'Driver object is single-use'); this.started = true;
    const m = this.manifest; await mkdir(m.artifactDir, { recursive: true });
    // Never reset/delete this intent after a crash; restart is read-only reconcile.
    await exclusiveJson(path.join(m.artifactDir, 'run-intent.json'), { ...this.state, pid: process.pid, startedAt: stamp(),
      nativeEffects: 'not_yet_observed', recovery: 'Exact readonly reconcile; never rerun this intent automatically' });
    await exclusiveJson(path.join(m.artifactDir, 'manifest.json'), m);
    this.beganAt = this.now(); this.state.startedAt = stamp(); this.state.phase = 'initializing';
    let failure;
    try {
      await this.save(); await this.initializeRoute(); await this.openThread();
      if (this.now() - this.beganAt >= m.bounds.walltimeMs) fail('RUNNER_DEADLINE', 'Deadline reached before model turn; no turn started');
      await this.startTurn(); const turn = await this.wait();
      this.state.result = { state: 'TERMINAL', nativeStatus: turn.status, terminalEventId: turn.terminalObservation ?? null,
        startedObserved: !!turn.started, historyCoverageIncomplete: this.state.lastTimeline?.coverage.complete !== true,
        humanOriginVerified: false, scientificAdoption: 'not_performed' };
    } catch (error) {
      failure = error; this.state.error = errorFact(error);
      let mayHaveStarted = !!this.state.turnId;
      if (this.state.startActionId) {
        try {
          const action = await this.callbacks.call('runtime-action', { args: ['--id', this.state.startActionId] });
          this.state.lastStartCorePhase = action.body?.phase;
          mayHaveStarted ||= !['prepared', 'invalidated', 'not_sent', 'rejected'].includes(action.body?.phase);
          if (action.turn_id) this.state.turnId = action.turn_id;
        } catch { mayHaveStarted = true; } // Unavailable durable readback preserves ambiguity.
      }
      this.state.result = { state: mayHaveStarted ? 'UNKNOWN' : 'STOPPED_BEFORE_TURN', noAutomaticRetry: true,
        scientificAdoption: 'not_performed', createdThreadId: this.state.threadId };
    } finally {
      try { this.state.closeReceipt = await this.adapter.close(); }
      catch (error) {
        this.state.cleanupError = errorFact(error);
        this.state.closeReceipt = { hostId: this.state.hostId, runtimeId: this.state.runtimeId,
          transport: { closed: false, exitObserved: false }, closedFactId: null, persistence: { persisted: false },
          meaning: 'Cleanup threw; process exit and closure fact are unconfirmed', error: this.state.cleanupError };
        failure ??= error;
      }
      this.state.endedAt = stamp(); this.state.elapsedMs = this.now() - this.beganAt; this.state.phase = 'ended';
      try { await this.log('ended', { result: this.state.result, error: this.state.error ?? null,
        transportClosed: this.state.closeReceipt.transport?.closed, closedFactId: this.state.closeReceipt.closedFactId }); }
      catch (error) { this.state.artifactError = errorFact(error); }
      await this.save();
    }
    return { ...clone(this.state), ...(failure ? { failure: true } : {}) };
  }
}

export async function submitControl({ manifest, manifestSha256, slotId, payload, callbacks, internalDeadline = false }) {
  const m = validateManifest(manifest); const slot = m.controls.find(item => item.id === slotId);
  if (!slot || (!internalDeadline && slot.trigger !== 'external') || (internalDeadline && slot.trigger !== 'deadline'))
    fail('OUT_OF_SCOPE', 'Control must use an exact frozen slot with the matching trigger');
  only(payload, ['reason', 'source_ref', 'origin_status', 'input_id', 'input_scope_revisions', 'correction'], 'control payload');
  boundedString(payload.reason, 'control reason');
  if (!payload.source_ref || typeof payload.source_ref !== 'object' || Buffer.byteLength(JSON.stringify(payload.source_ref)) > 8192)
    fail('CONTROL_SOURCE_REQUIRED', 'Actual bounded source reference required; no human origin inferred');
  if (payload.origin_status === 'human_direct') fail('CONTROL_SOURCE_REQUIRED', 'Driver cannot assert human_direct; provide an accurately verified B02 input reference to State');
  if (slot.kind === 'correct') {
    integer(payload.input_id, 1, Number.MAX_SAFE_INTEGER, 'correction input_id');
    const c = payload.correction; only(c, ['expectedTurnId', 'input', 'clientUserMessageId'], 'correction'); exactId(c.expectedTurnId, 'expected turnId'); exactId(c.clientUserMessageId, 'correction client ID');
    if (!Array.isArray(c.input) || !c.input.length || c.input.length > 16 || c.input.some(x => x.type !== 'text' || typeof x.text !== 'string') || Buffer.byteLength(JSON.stringify(c.input)) > 32768)
      fail('INVALID_MANIFEST', 'Bounded explicit correction text required');
  } else if (payload.correction || payload.input_id) fail('INVALID_MANIFEST', 'Pause does not send correction content');
  const prior = await boundedJson(path.join(m.artifactDir, 'run-intent.json'));
  if (prior.runId !== m.runId || prior.manifestSha256 !== manifestSha256 || prior.hostId !== m.native.hostId || prior.taskId !== m.taskId)
    fail('IDENTITY_MISMATCH', 'Control run identity differs from frozen manifest');
  const state = callbacks ?? stateCallbacks(m.state);
  const status = await boundedJson(path.join(m.artifactDir, 'status.json'));
  if (status.manifestSha256 !== manifestSha256 || status.runtimeId !== prior.runtimeId || status.phase === 'ended' || !status.turnId || !status.threadId)
    fail('STALE_TURN', 'Explicit control requires this run still owning an exact acknowledged turn');
  const overview = await state.call('runtime-overview', { args: ['--work', m.workId, '--limit', '100'] });
  const current = overview.tasks?.find(item => item.id === m.taskId && item.work_id === m.workId)?.route;
  if (!current || current.host_id !== status.hostId || current.runtime_id !== status.runtimeId || current.thread_id !== status.threadId)
    fail('IDENTITY_MISMATCH', 'Current core route differs from this runner; control withheld');
  if (slot.kind === 'correct' && payload.correction.expectedTurnId !== status.turnId)
    fail('STALE_TURN', 'Correction target differs from the acknowledged current turn');
  const request = { controlId: slot.id, kind: slot.kind, manifestSha256, source: m.source, requestedAt: stamp(), payload };
  // Exclusive record means a timeout/crash is inspected, never silently retried.
  await exclusiveJson(path.join(m.artifactDir, `control-${slot.id}.json`), request);
  const plan = await state.call('runtime-scope', { args: ['--task', m.taskId, '--no-dependents'] });
  if (plan.tasks?.length !== 1 || plan.tasks[0] !== m.taskId || plan.include_dependents !== false) fail('OUT_OF_SCOPE', 'Control plan must remain exactly this task');
  const body = { plan, owner: m.owner.id, owner_revisions: { [m.workId]: m.owner.revision }, reason: payload.reason,
    source_ref: clone(payload.source_ref), origin_status: payload.origin_status ?? 'unverified',
    expected_native_target: { task_id: m.taskId, host_id: status.hostId, runtime_id: status.runtimeId,
      thread_id: status.threadId, turn_id: status.turnId },
    ...(slot.kind === 'correct' ? { input_id: payload.input_id, input_scope_revisions: payload.input_scope_revisions ?? {}, driver_correction: clone(payload.correction) } : {}) };
  const receipt = await state.call('runtime-control', { args: ['--id', slot.id, '--kind', slot.kind, '--source', m.source], body });
  await exclusiveJson(path.join(m.artifactDir, `control-${slot.id}-receipt.json`), { ...request, receipt, returnedAt: stamp(),
    meaning: 'Core scoped admission; native delivery/effect require separate exact action facts' });
  return receipt;
}

/** Restart never recreates the original action or claims the old runtime. */
export async function reconcileRun({ manifest, manifestSha256, callbacks, transport, versionEvidence }) {
  const m = validateManifest(manifest), saved = await boundedJson(path.join(m.artifactDir, 'status.json'));
  if (saved.manifestSha256 !== manifestSha256 || saved.runId !== m.runId || saved.hostId !== m.native.hostId || !saved.threadId)
    fail('IDENTITY_MISMATCH', 'Exact prior run/thread identity required for readonly recovery');
  const state = callbacks ?? stateCallbacks(m.state), adapter = new CodexAdapter({ hostId: m.native.hostId, command: m.native.command,
    processCwd: m.native.processCwd, allowedCwds: m.native.allowedCwds, allowedThreadIds: [saved.threadId],
    transport, versionEvidence, factSink: state.factSink });
  let result;
  try {
    const session = await state.call('runtime-action', { args: ['--id', m.runId + '-session'] });
    if (session.task_id !== m.taskId || session.host_id !== m.native.hostId || session.runtime_id !== saved.runtimeId ||
        session.thread_id !== saved.threadId || !session.body?.ack_event || !session.body?.attempt_event)
      fail('IDENTITY_MISMATCH', 'Readonly recovery thread must match the actual persisted session ACK, not a caller-edited status');
    if (saved.anchorProof) {
      const action = await state.call('runtime-action', { args: ['--id', saved.anchorProof.actionId] });
      if (action.host_id !== saved.hostId || action.thread_id !== saved.threadId || action.turn_id !== saved.anchorProof.turnId || !action.body?.ack_event || !action.body?.attempt_event)
        fail('ANCHOR_UNVERIFIED', 'Recovery anchor differs from the real durable acknowledged turn');
    }
    await adapter.connect();
    const timeline = await adapter.timeline({ threadId: saved.threadId, anchor: saved.anchorProof ? { turnId: saved.anchorProof.turnId } : null, ...m.timeline });
    const exact = await adapter.reconcile({ threadId: saved.threadId, ...(saved.turnId ? { turnId: saved.turnId } : { clientUserMessageId: m.turn.clientUserMessageId }), maxPages: m.timeline.maxPages ?? 3, pageSize: m.timeline.pageSize ?? 10 });
    result = { priorRuntimeId: saved.runtimeId, observerRuntimeId: adapter.runtimeId, timeline, exact,
      ownershipTransferred: false, originalActionRetried: false, scientificAdoption: 'not_performed' };
  } finally { const cleanup = await adapter.close(); if (result) result.observerCleanup = cleanup; }
  return result;
}

export async function readRunStatus({ manifest, manifestSha256 }) {
  const m = validateManifest(manifest), value = await boundedJson(path.join(m.artifactDir, 'status.json'));
  if (value.runId !== m.runId || value.manifestSha256 !== manifestSha256) fail('IDENTITY_MISMATCH', 'Run status belongs to another frozen manifest');
  return value;
}
