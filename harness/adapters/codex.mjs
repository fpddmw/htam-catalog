import { EventEmitter } from 'node:events';
import path from 'node:path';
import { randomUUID } from 'node:crypto';
import { AdapterError, StdioTransport, cliVersion, stamp } from './transport.mjs';
import { fingerprint, normalizeNotification, TurnLedger, terminalStatus, waitForLedger } from './events.mjs';
import { observeTimeline } from './timeline.mjs';

export const ADAPTER_VERSION = '1.1.0';
export const TESTED_CLI_VERSION = '0.163.0-alpha.10.2';
const id = (value, label) => {
  if (typeof value !== 'string' || !/^[A-Za-z0-9_-]{1,160}$/.test(value))
    throw new AdapterError('INVALID', `An exact ${label} is required`);
  return value;
};
const copy = value => JSON.parse(JSON.stringify(value));
const cwdKey = value => (/^(?:[A-Za-z]:[\\/]|\\\\)/.test(value) ? path.win32.normalize(value).toLowerCase() : path.posix.normalize(value));
// Permission equivalence is pinned to the public v2 schema of TESTED_CLI_VERSION.
// Unknown fields fail closed. Empty requested arrays must never mean "any roots".
function sandboxSemantics(policy, requireExplicit = false) {
  const fields = { readOnly: ['type', 'networkAccess'], dangerFullAccess: ['type'],
    workspaceWrite: ['type', 'networkAccess', 'writableRoots', 'excludeSlashTmp', 'excludeTmpdirEnvVar'] };
  if (!policy || typeof policy !== 'object' || !fields[policy.type] ||
      Object.keys(policy).some(key => !fields[policy.type].includes(key))) return null;
  const defaultsApplied = []; const value = { type: policy.type };
  if (policy.type !== 'dangerFullAccess') {
    for (const key of fields[policy.type].filter(key => key !== 'type' && key !== 'writableRoots')) {
      if (policy[key] === undefined) {
        if (requireExplicit) return null;
        value[key] = false; defaultsApplied.push(key);
      } else if (typeof policy[key] === 'boolean') value[key] = policy[key];
      else return null;
    }
  }
  if (policy.type === 'workspaceWrite') {
    const roots = policy.writableRoots;
    if (roots === undefined && requireExplicit) return null;
    if (roots !== undefined && (!Array.isArray(roots) || roots.some(root => typeof root !== 'string' ||
        !(path.win32.isAbsolute(root) || path.posix.isAbsolute(root))))) return null;
    if (roots === undefined) defaultsApplied.push('writableRoots');
    value.writableRoots = [...new Set((roots ?? []).map(cwdKey))].sort();
  }
  return { value, defaultsApplied, schemaVersion: TESTED_CLI_VERSION,
    implicitScope: policy.type === 'workspaceWrite'
      ? 'native workspaceWrite includes cwd and native temp defaults unless explicitly excluded'
      : policy.type === 'dangerFullAccess' ? 'unrestricted native permissions explicitly requested' : 'native readOnly scope' };
}
function compareSandbox(actual, expected) {
  const observed = sandboxSemantics(actual); const requested = sandboxSemantics(expected, true);
  return { matches: !!observed && !!requested && fingerprint(observed.value) === fingerprint(requested.value),
    requested: requested ?? { unsupportedOrIncomplete: true, raw: expected },
    observed: observed ?? { unknownFieldsOrInvalid: true, raw: actual } };
}
const safeThread = thread => Object.fromEntries(['id', 'name', 'cwd', 'model', 'reasoningEffort',
  'createdAt', 'updatedAt', 'status', 'ephemeral', 'source', 'canAcceptDirectInput', 'cliVersion']
  .filter(k => thread[k] !== undefined).map(k => [k, thread[k]]));

export class CodexAdapter extends EventEmitter {
  constructor({ hostId, command = 'codex', processCwd, allowedThreadIds = [], allowedCwds = [],
    guard, factSink, transport, versionEvidence, maxQueuedFacts = 256, maxActions = 1024 } = {}) {
    super(); id(hostId, 'hostId');
    this.hostId = hostId; this.runtimeId = randomUUID(); this.command = command; this.processCwd = processCwd;
    this.allowedThreadIds = new Set(allowedThreadIds.map(x => id(x, 'threadId')));
    this.allowedCwds = new Set(allowedCwds.map(cwdKey));
    this.guard = guard; this.factSink = factSink; this.versionEvidence = versionEvidence;
    this.transport = transport ?? new StdioTransport({ command, cwd: processCwd });
    this.ledger = new TurnLedger(); this.owned = new Map(); this.actions = new Map(); this.requests = new Map();
    this.unknownThreads = new Set(); this.connected = false; this.closed = false; this.busy = false;
    this.queueCount = 0; this.maxQueuedFacts = maxQueuedFacts; this.maxActions = maxActions;
    this.persistQueue = Promise.resolve(); this.persistenceError = null;
    this.lastObservedAt = null; this.lastPersistedAt = null;
    this.transport.on('notification', message => this.onNotification(message));
    this.transport.on('late_response', receipt => this.onLateResponse(receipt));
    this.transport.on('disconnect', facts => {
      this.connected = false;
      for (const [threadId] of this.owned) {
        if ([...this.ledger.turns.values()].some(t => t.threadId === threadId && t.owned && !t.terminal))
          this.unknownThreads.add(threadId);
      }
      this.emit('disconnect', facts); this.record(this.fact('runtime_disconnected', facts));
    });
    this.transport.on('server_request_rejected', facts => this.record(this.fact('client_request_unsupported', facts)));
  }
  fact(kind, facts = {}) {
    const result = { formatVersion: '1.0', adapter: 'codex.app-server.stdio', adapterVersion: ADAPTER_VERSION,
      hostId: this.hostId, runtimeId: this.runtimeId, source: 'adapter_observation', kind,
      observedAt: stamp(), ...facts };
    result.eventId ??= 'adapter:' + fingerprint(result); return result;
  }
  record(fact, required = false) {
    this.lastObservedAt = fact.observedAt; this.emit('fact', copy(fact));
    if (!this.factSink) return required ? Promise.reject(new AdapterError('PERSISTENCE_REQUIRED', 'A durable fact sink is required')) : Promise.resolve({ persisted: false });
    if (this.persistenceError) return required ? Promise.reject(this.persistenceError) : Promise.resolve({ persisted: false });
    if (this.queueCount >= this.maxQueuedFacts) {
      this.persistenceError = new AdapterError('PERSISTENCE_FAILED', 'Fact queue bound reached; new mutations disabled');
      this.emit('persistence_failed', this.persistenceError.toJSON());
      return required ? Promise.reject(this.persistenceError) : Promise.resolve({ persisted: false });
    }
    this.queueCount++;
    const operation = this.persistQueue.then(async () => {
      if (this.persistenceError) throw this.persistenceError;
      const receipt = await this.factSink(copy(fact));
      if (receipt?.persisted !== true || typeof receipt.eventId !== 'string' || !receipt.eventId)
        throw new AdapterError('PERSISTENCE_FAILED', 'Fact sink did not confirm durable storage');
      this.lastPersistedAt = stamp(); return receipt;
    }).catch(error => {
      this.persistenceError = error instanceof AdapterError ? error : new AdapterError('PERSISTENCE_FAILED', 'Fact sink failed; new mutations disabled');
      this.emit('persistence_failed', this.persistenceError.toJSON()); throw this.persistenceError;
    }).finally(() => { this.queueCount--; });
    this.persistQueue = operation.catch(() => {});
    if (required) return operation;
    operation.catch(() => {}); return operation.catch(() => ({ persisted: false }));
  }
  async flush() { await this.persistQueue; if (this.persistenceError) throw this.persistenceError; }
  capabilities() {
    const compatible = this.versionEvidence?.version === TESTED_CLI_VERSION;
    return { formatVersion: '1.0', adapterVersion: ADAPTER_VERSION, hostId: this.hostId,
      runtimeId: this.runtimeId, transport: 'public_single_stdio', nativeVersion: this.versionEvidence ?? null,
      compatible, connected: this.connected, lastObservedAt: this.lastObservedAt, lastPersistedAt: this.lastPersistedAt,
      coverage: { exactRead: 'implemented', boundedReconcile: 'implemented',
        boundedTimeline: 'implemented_exact_authorized_thread_persisted_input_unverified_origin',
        hostIdentitySource: 'explicit_integrator_configuration_local_process_platform_observed',
        ownedCreateStartResume: compatible ? 'implemented_guard_and_persistence_required' : 'UNSUPPORTED_VERSION',
        ownedInterrupt: 'implemented_waits_for_matching_started', ownedSteer: 'schema_supported_fake_tested_native_behavior_unverified',
        otherRuntimeControl: 'UNSUPPORTED', dotWakeup: 'UNSUPPORTED', sharedRuntimeEvents: 'UNSUPPORTED',
        requestIdempotency: 'not_supported', clientUserMessageId: 'correlation_only',
        lifecycle: ['prepared', 'send_attempted', 'acknowledged', 'started_observed', 'terminal_observed'],
        readScope: [...this.allowedThreadIds], globalSessionEnumeration: false,
        guardCoverage: 'only_mutations_through_this_adapter', uiRoute: 'generated_exact_ID_not_UI_verified' },
      persistenceHealthy: !this.persistenceError, observation: 'Adapter coverage is not an end-to-end Dot integration claim' };
  }
  initializeParams() {
    return { clientInfo: { name: 'htam_native_adapter', title: 'HTAM public native adapter', version: ADAPTER_VERSION } };
  }
  async connect() {
    if (this.closed || this.connected) throw new AdapterError('CLOSED', 'Adapter cannot connect twice');
    this.versionEvidence ??= cliVersion({ command: this.command, cwd: this.processCwd });
    if (!this.versionEvidence.available) throw new AdapterError('UNAVAILABLE', 'Codex CLI version command failed', { versionEvidence: this.versionEvidence });
    this.transport.open();
    const response = await this.transport.request('initialize', this.initializeParams());
    this.transport.notify('initialized', {}); this.connected = true;
    // Do not retain codexHome or other private configuration paths in diagnostics.
    this.initializeReceipt = { userAgent: response.result?.userAgent, platformFamily: response.result?.platformFamily,
      platformOs: response.result?.platformOs };
    await this.record(this.fact('runtime_initialized', { nativeVersion: this.versionEvidence.version,
      initialize: this.initializeReceipt, transportScope: 'own_single_stdio_runtime' }));
    return this.capabilities();
  }
  scope(threadId) {
    id(threadId, 'threadId');
    if (!this.allowedThreadIds.has(threadId)) throw new AdapterError('OUT_OF_SCOPE', 'Thread ID is outside the explicit read scope', { threadId });
  }
  entry(threadId) { return { hostId: this.hostId, threadId,
    uri: `codex://threads/${encodeURIComponent(threadId)}`, routeVerified: false,
    nativeRead: { method: 'thread/read', params: { threadId, includeTurns: false } } }; }
  async readThread(threadId) {
    this.scope(threadId);
    const response = await this.transport.request('thread/read', { threadId, includeTurns: false });
    if (response.result?.thread?.id !== threadId) throw new AdapterError('IDENTITY_MISMATCH', 'Native read returned another thread', { requestedThreadId: threadId });
    const fact = this.fact('thread_read', { threadId, requestId: response.requestId,
      thread: safeThread(response.result.thread), observationScope: 'stored_metadata_and_this_runtime_status' });
    await this.record(fact);
    return { ...fact, entry: this.entry(threadId), persistence: 'public_read_observed_not_restart_verified' };
  }
  async timeline(options) { return observeTimeline(this, options); }
  async reconcile({ threadId, turnId, clientUserMessageId, maxPages = 3, pageSize = 20 } = {}) {
    this.scope(threadId); if (turnId) id(turnId, 'turnId');
    if (!turnId && !clientUserMessageId) throw new AdapterError('INVALID', 'Reconcile requires an exact turnId or clientUserMessageId');
    if (!Number.isInteger(maxPages) || maxPages < 1 || maxPages > 10 || !Number.isInteger(pageSize) || pageSize < 1 || pageSize > 100)
      throw new AdapterError('INVALID', 'Reconcile bounds must be explicit finite pages and page size');
    const metadata = await this.readThread(threadId);
    let cursor, exhausted = false, pages = 0, latestObservedTurnId = null; const candidates = new Map(); const seenCursors = new Set();
    do {
      const response = await this.transport.request('thread/turns/list', { threadId, limit: pageSize,
        sortDirection: 'desc', itemsView: 'summary', ...(cursor ? { cursor } : {}) });
      const result = response.result;
      if (!Array.isArray(result?.data) || (result.threadId && result.threadId !== threadId))
        throw new AdapterError('IDENTITY_MISMATCH', 'Invalid exact-thread turn response', { threadId });
      pages++;
      if (pages === 1) latestObservedTurnId = result.data[0]?.id ?? null;
      for (const turn of result.data) {
        if (typeof turn.id !== 'string' || (turn.threadId && turn.threadId !== threadId))
          throw new AdapterError('IDENTITY_MISMATCH', 'Turn response contains a mismatched identity', { threadId });
        const clientIds = (turn.items ?? []).filter(item => item.type === 'userMessage').map(item => item.clientId).filter(Boolean);
        if ((turnId && turn.id === turnId) || (!turnId && clientIds.includes(clientUserMessageId)))
          candidates.set(turn.id, { turnId: turn.id, status: turn.status, startedAt: turn.startedAt ?? null,
            completedAt: turn.completedAt ?? null, clientUserMessageIds: clientIds,
            error: turn.error ? { message: String(turn.error.message ?? '').slice(0, 1024) } : null });
      }
      cursor = result.nextCursor; exhausted = !cursor;
      if (cursor && seenCursors.has(cursor)) throw new AdapterError('PROTOCOL_ERROR', 'Turn pagination repeated a cursor');
      if (cursor) seenCursors.add(cursor);
      // Exact native turn identity can stop once found. Client ID correlation scans the full declared bound for ambiguity.
      if (turnId && candidates.has(turnId)) break;
    } while (!exhausted && pages < maxPages);
    const matches = [...candidates.values()];
    const selected = matches.length === 1 ? matches[0] : null;
    const uniquelyResolved = !!selected && (!!turnId || exhausted);
    const result = { threadId, requestedTurnId: turnId ?? null, clientUserMessageId: clientUserMessageId ?? null,
      state: uniquelyResolved ? (terminalStatus(selected.status) ? 'TERMINAL' : 'OBSERVED') : 'UNKNOWN',
      turn: selected, candidates: matches.length > 1 ? matches : undefined,
      coverage: { pages, pageSize, maxPages, exhausted, found: !!selected, ambiguous: matches.length > 1,
        identityResolvedWithinCoverage: uniquelyResolved,
        scope: 'exact_thread_public_read_only', liveStateScope: 'this_stdio_runtime', noAutomaticRetry: true },
      latestObservedTurnId, threadStatus: metadata.thread.status, entry: this.entry(threadId), observedAt: stamp() };
    if (selected && uniquelyResolved) {
      const fact = this.fact('turn_reconciled', { threadId, turnId: selected.turnId, nativeStatus: selected.status,
        source: 'public_app_server_readback', requestedTurnId: turnId ?? null,
        requestedClientUserMessageId: clientUserMessageId ?? null,
        matchedClientUserMessageIds: selected.clientUserMessageIds,
        latestObservedTurnId,
        identityResolvedWithinCoverage: uniquelyResolved, coverage: result.coverage });
      const observation = this.ledger.accept(fact);
      if (observation.turn?.conflict) {
        result.state = 'UNKNOWN'; result.conflictingObservations = true;
        this.unknownThreads.add(threadId);
      }
      await this.record(fact); this.emit('turn', fact);
      result.fact = copy(fact);
      // Only the exact attempted action can resolve UNKNOWN. An older completed
      // turn on the same thread is not evidence about an ambiguous newer send.
      if (terminalStatus(selected.status)) {
        for (const action of this.actions.values()) {
          if (action.threadId === threadId && ((action.method === 'turn/start' &&
              (action.nativeTurnId === selected.turnId || selected.clientUserMessageIds.includes(action.clientUserMessageId))) ||
              (['turn/steer', 'turn/interrupt'].includes(action.method) && action.turnId === selected.turnId)))
            action.state = 'reconciled_terminal';
        }
        const unresolved = [...this.actions.values()].some(action => action.threadId === threadId && action.state === 'unknown');
        const conflicting = [...this.ledger.turns.values()].some(turn => turn.threadId === threadId && turn.conflict);
        if (!unresolved && !conflicting) this.unknownThreads.delete(threadId);
      }
    } else {
      const fact = this.fact('reconcile_unknown', result);
      await this.record(fact); result.fact = copy(fact);
    }
    return result;
  }
  settings(settings) {
    if (!settings || !['model', 'reasoningEffort', 'cwd', 'sandboxMode', 'sandboxPolicy', 'approvalPolicy'].every(k => settings[k] !== undefined))
      throw new AdapterError('INVALID', 'Explicit model, reasoningEffort, cwd, sandboxMode, sandboxPolicy and approvalPolicy are required');
    if (typeof settings.model !== 'string' || !settings.model || typeof settings.reasoningEffort !== 'string' || !settings.reasoningEffort)
      throw new AdapterError('INVALID', 'Model and reasoning effort must be explicit');
    if (!this.allowedCwds.has(cwdKey(settings.cwd))) throw new AdapterError('OUT_OF_SCOPE', 'cwd is not in the exact allowed cwd set');
    const types = { 'read-only': 'readOnly', 'workspace-write': 'workspaceWrite', 'danger-full-access': 'dangerFullAccess' };
    if (!types[settings.sandboxMode] || settings.sandboxPolicy?.type !== types[settings.sandboxMode])
      throw new AdapterError('INVALID', 'sandboxMode and sandboxPolicy must agree');
    if (!sandboxSemantics(settings.sandboxPolicy, true))
      throw new AdapterError('INVALID', 'Permission fields must be explicit and supported by the tested public schema');
    return copy(settings);
  }
  ownedThread(threadId) {
    this.scope(threadId); const binding = this.owned.get(threadId);
    if (!binding || !binding.settingsVerified) throw new AdapterError('UNSUPPORTED', 'Control requires a verified thread owned by this exact runtime', { hostId: this.hostId, runtimeId: this.runtimeId, threadId });
    return binding;
  }
  verifySettings(response, settings) {
    return response.model === settings.model && response.reasoningEffort === settings.reasoningEffort &&
      cwdKey(response.cwd ?? '') === cwdKey(settings.cwd) &&
      fingerprint(response.approvalPolicy) === fingerprint(settings.approvalPolicy) && compareSandbox(response.sandbox, settings.sandboxPolicy).matches;
  }
  input(input) {
    if (!Array.isArray(input) || !input.length || input.some(item => item?.type !== 'text' || typeof item.text !== 'string'))
      throw new AdapterError('INVALID', 'This adapter version accepts explicit text input items');
    return copy(input);
  }
  async perform(method, params, intent, facts = {}, preflight) {
    if (!this.connected || this.closed) throw new AdapterError('CLOSED', 'Adapter is not connected');
    if (this.versionEvidence?.version !== TESTED_CLI_VERSION) throw new AdapterError('UNSUPPORTED_VERSION', 'Mutations require the tested native version');
    if (!this.guard || !this.factSink) throw new AdapterError('GUARD_REQUIRED', 'Mutations require the management guard and durable intent/fact sink');
    if (this.busy) throw new AdapterError('BUSY', 'Another mutation is still awaiting acknowledgement');
    if (!intent || typeof intent.intentId !== 'string' || !intent.intentId || !Number.isInteger(intent.revision) || intent.revision < 1)
      throw new AdapterError('INVALID', 'A durable intent identity and revision are required');
    if (this.actions.has(intent.intentId)) throw new AdapterError('ALREADY_ATTEMPTED', 'Intent was already attempted; reconcile rather than resend');
    if (this.actions.size >= this.maxActions) throw new AdapterError('CAPACITY', 'Action tracking bound reached; persist and rotate an idle runtime');
    this.busy = true;
    const action = { intentId: intent.intentId, intentRevision: intent.revision, method,
      threadId: params.threadId ?? null, turnId: params.turnId ?? params.expectedTurnId ?? null,
      clientUserMessageId: params.clientUserMessageId ?? null, requestSha256: fingerprint(params), state: 'prepared', ...facts };
    action.dispatchKey = intent.dispatchKey ?? intent.intentId;
    action.workId = intent.workId ?? null;
    try {
      await this.flush();
      const response = await this.transport.request(method, params, { beforeSend: async ({ requestId }) => {
        const guardRequest = { ...action, hostId: this.hostId, runtimeId: this.runtimeId, intent: copy(intent) };
        const guardReceipt = await this.guard(copy(guardRequest));
        if (guardReceipt?.allowed !== true || guardReceipt.intentId !== intent.intentId ||
            guardReceipt.requestSha256 !== action.requestSha256 || !guardReceipt.guardRef)
          throw new AdapterError('GUARD_DENIED', 'Current guard did not approve this exact intent and request');
        if (method === 'thread/resume' && guardReceipt.closedOwnedRuntimeVerified !== true)
          throw new AdapterError('UNSUPPORTED', 'Resume requires verified closed prior ownership; external active runtimes are unsupported');
        action.requestId = requestId; action.guardRef = guardReceipt.guardRef; action.state = 'attempted';
        this.actions.set(intent.intentId, action); this.requests.set(requestId, action);
        const persisted = await this.record(this.fact('send_attempted', { ...action,
          requestIdMeaning: 'connection_correlation_not_server_idempotency', noAutomaticRetry: true }), true);
        action.intentFactId = persisted.eventId;
        preflight?.();
      } });
      // Acknowledgements are native facts, but a wrong target cannot become our
      // binding. Mark the attempted action unknown and retain its original IDs.
      if (method === 'thread/start' || method === 'thread/resume') {
        id(response.result?.thread?.id, 'native threadId');
        if (method === 'thread/resume' && response.result.thread.id !== params.threadId)
          throw new AdapterError('IDENTITY_MISMATCH', 'Resume acknowledgement returned another thread', { mayHaveBeenSent: true });
      }
      if (method === 'turn/start') {
        id(response.result?.turn?.id, 'native turnId');
        if (response.result.threadId && response.result.threadId !== params.threadId)
          throw new AdapterError('IDENTITY_MISMATCH', 'Start acknowledgement returned another thread', { mayHaveBeenSent: true });
      }
      if (method === 'turn/steer' && response.result?.turnId !== params.expectedTurnId)
        throw new AdapterError('IDENTITY_MISMATCH', 'Steer acknowledgement returned another turn', { mayHaveBeenSent: true });
      action.acknowledgedAt = response.acknowledgedAt; action.state = 'acknowledged';
      if (method === 'turn/start') action.nativeTurnId = response.result?.turn?.id ?? null;
      await this.record(this.fact('acknowledged', { ...action, nativeResult: this.ackProjection(method, response.result) }), true);
      return { ...response, action: copy(action) };
    } catch (error) {
      const failure = error instanceof AdapterError ? error : new AdapterError('UNKNOWN', 'Mutation failed after preparation; reconcile before another action', { mayHaveBeenSent: true });
      if (action.requestId !== undefined) {
        action.state = failure.facts.mayHaveBeenSent === false ? 'not_sent' : (failure.code === 'REJECTED' ? 'rejected' : 'unknown');
        if (failure.code !== 'REJECTED' && failure.facts.mayHaveBeenSent !== false && action.threadId)
          this.unknownThreads.add(action.threadId);
        await this.record(this.fact(action.state === 'not_sent' ? 'action_not_sent' : (failure.code === 'REJECTED' ? 'native_rejected' : 'action_unknown'),
          { ...action, error: failure.toJSON(), noAutomaticRetry: true }));
      }
      throw failure;
    } finally { this.busy = false; }
  }
  ackProjection(method, result) {
    if (method.startsWith('thread/')) return { threadId: result?.thread?.id, model: result?.model,
      reasoningEffort: result?.reasoningEffort, cwd: result?.cwd, sandbox: result?.sandbox,
      approvalPolicy: result?.approvalPolicy, instructionSources: result?.instructionSources,
      sandboxSemantics: sandboxSemantics(result?.sandbox) ?? { unknownFieldsOrInvalid: true },
      persistence: 'identity_acknowledged_persistence_unconfirmed' };
    return { turnId: result?.turn?.id ?? result?.turnId, nativeStatus: result?.turn?.status };
  }
  async createThread({ settings: desired, intent, baseInstructions, developerInstructions, ephemeral = false } = {}) {
    const settings = this.settings(desired);
    const params = { model: settings.model, cwd: settings.cwd, sandbox: settings.sandboxMode,
      approvalPolicy: settings.approvalPolicy, ephemeral, config: { model_reasoning_effort: settings.reasoningEffort },
      ...(baseInstructions !== undefined ? { baseInstructions } : {}),
      ...(developerInstructions !== undefined ? { developerInstructions } : {}) };
    const response = await this.perform('thread/start', params, intent, { requestedSettings: settings });
    return this.bindResponse(response, settings);
  }
  bindResponse(response, settings, expectedThreadId) {
    const threadId = id(response.result?.thread?.id, 'native threadId');
    if (expectedThreadId && threadId !== expectedThreadId) throw new AdapterError('IDENTITY_MISMATCH', 'Resume returned another thread');
    this.allowedThreadIds.add(threadId);
    const verified = this.verifySettings(response.result, settings);
    this.owned.set(threadId, { threadId, hostId: this.hostId, runtimeId: this.runtimeId,
      settings, settingsVerified: verified, ownershipIntentId: response.action.intentId });
    if (!verified) throw new AdapterError('SETTINGS_MISMATCH', 'Native settings differ; no model turn may start',
      { threadId, requested: settings, observed: this.ackProjection('thread/start', response.result) });
    return { threadId, hostId: this.hostId, runtimeId: this.runtimeId, action: response.action,
      effectiveSettings: this.ackProjection('thread/start', response.result),
      permissionVerification: compareSandbox(response.result.sandbox, settings.sandboxPolicy),
      entry: this.entry(threadId), persistence: 'identity_acknowledged_persistence_unconfirmed' };
  }
  async resumeThread({ threadId, settings: desired, intent, priorOwnership } = {}) {
    this.scope(threadId); const settings = this.settings(desired);
    if (!priorOwnership || priorOwnership.threadId !== threadId || priorOwnership.hostId !== this.hostId ||
        !priorOwnership.runtimeId || priorOwnership.runtimeId === this.runtimeId || !priorOwnership.closedFactId || !priorOwnership.terminalTurnId)
      throw new AdapterError('UNSUPPORTED', 'Only a completed previously owned and closed runtime can be resumed; arbitrary external runtime takeover is unsupported');
    const check = await this.reconcile({ threadId, turnId: priorOwnership.terminalTurnId });
    if (check.state !== 'TERMINAL' || check.latestObservedTurnId !== priorOwnership.terminalTurnId)
      throw new AdapterError('UNKNOWN', 'Latest prior terminal ownership could not be reconciled');
    const response = await this.perform('thread/resume', { threadId, excludeTurns: true,
      model: settings.model, cwd: settings.cwd, sandbox: settings.sandboxMode,
      approvalPolicy: settings.approvalPolicy, config: { model_reasoning_effort: settings.reasoningEffort } },
    intent, { requestedSettings: settings, priorOwnership: copy(priorOwnership) });
    if (response.result?.thread?.turns?.some(t => !terminalStatus(t.status)))
      throw new AdapterError('UNKNOWN', 'Resume reported activity; control is withheld until exact reconciliation');
    return this.bindResponse(response, settings, threadId);
  }
  async startTurn({ threadId, settings: desired, input, clientUserMessageId, intent } = {}) {
    this.ownedThread(threadId); const settings = this.settings(desired); id(clientUserMessageId, 'clientUserMessageId');
    if (this.unknownThreads.has(threadId) || [...this.ledger.turns.values()].some(t => t.threadId === threadId && (t.conflict || (t.owned && !t.terminal))))
      throw new AdapterError('UNKNOWN', 'Prior activity must be reconciled before a new turn');
    const response = await this.perform('turn/start', { threadId, model: settings.model,
      effort: settings.reasoningEffort, cwd: settings.cwd, sandboxPolicy: settings.sandboxPolicy,
      approvalPolicy: settings.approvalPolicy, clientUserMessageId, input: this.input(input) }, intent, { requestedSettings: settings });
    if ((response.result.threadId && response.result.threadId !== threadId) || !response.result.turn?.id)
      throw new AdapterError('IDENTITY_MISMATCH', 'Turn acknowledgement identity mismatch');
    const turnId = id(response.result.turn.id, 'turnId');
    this.ledger.acknowledge(threadId, turnId, { intentId: intent.intentId, clientUserMessageId, requestedSettings: settings });
    this.emit('turn', { threadId, turnId });
    return { ...this.ledger.get(threadId, turnId), action: response.action, entry: this.entry(threadId),
      executionSettings: { requested: settings, confirmedByTurnResponse: false,
        basis: 'explicit native overrides; initial thread settings separately confirmed' } };
  }
  async liveTurn(threadId, turnId, timeoutMs) {
    this.ownedThread(threadId); id(turnId, 'turnId');
    if (this.unknownThreads.has(threadId)) throw new AdapterError('UNKNOWN', 'An ambiguous action must be reconciled before further control');
    const turn = this.ledger.get(threadId, turnId);
    if (!turn?.owned) throw new AdapterError('UNSUPPORTED', 'Control requires this runtime\'s exact acknowledged turn');
    const observed = await waitForLedger(this, threadId, turnId, t => t.started || t.terminal || t.conflict, timeoutMs);
    await this.flush();
    if (observed.conflict) throw new AdapterError('UNKNOWN', 'Conflicting native facts require reconciliation');
    return this.ledger.get(threadId, turnId);
  }
  exactLivePreflight(threadId, turnId) {
    const current = this.ledger.get(threadId, turnId);
    if (!current?.owned || !current.started || current.terminal || current.conflict)
      throw new AdapterError('STALE_TURN', 'Exact owned turn is no longer in flight; no control request sent', { mayHaveBeenSent: false, threadId, turnId });
  }
  async interrupt({ threadId, turnId, intent, timeoutMs = 20000 } = {}) {
    const turn = await this.liveTurn(threadId, turnId, timeoutMs);
    if (turn.terminal) return { threadId, turnId, skipped: 'already_terminal', turn };
    const response = await this.perform('turn/interrupt', { threadId, turnId }, intent, {}, () => this.exactLivePreflight(threadId, turnId));
    return { threadId, turnId, action: response.action, acknowledged: true, terminalConfirmed: false,
      meaning: 'Native interrupt acknowledgement; wait for matching turn terminal or reconcile' };
  }
  async steer({ threadId, turnId, input, clientUserMessageId, intent, timeoutMs = 20000 } = {}) {
    const turn = await this.liveTurn(threadId, turnId, timeoutMs); id(clientUserMessageId, 'clientUserMessageId');
    if (turn.terminal) throw new AdapterError('STALE_TURN', 'Turn already terminal; steer was not sent');
    const response = await this.perform('turn/steer', { threadId, expectedTurnId: turnId,
      input: this.input(input), clientUserMessageId }, intent, {}, () => this.exactLivePreflight(threadId, turnId));
    if (response.result?.turnId !== turnId) throw new AdapterError('IDENTITY_MISMATCH', 'Steer acknowledgement returned another turn');
    return { threadId, turnId, action: response.action, acknowledged: true, correctionApplied: 'unverified' };
  }
  async waitTerminal(threadId, turnId, timeoutMs = 20000) {
    this.scope(threadId); id(turnId, 'turnId');
    const turn = await waitForLedger(this, threadId, turnId, t => t.terminal || t.conflict, timeoutMs);
    await this.flush(); if (turn.conflict) throw new AdapterError('UNKNOWN', 'Conflicting terminal events'); return turn;
  }
  onNotification(message) {
    const threadId = message.params?.threadId ?? message.params?.thread?.id;
    if (!this.allowedThreadIds.has(threadId)) return;
    try {
      const fact = normalizeNotification(message, this.hostId, this.runtimeId);
      if (!fact) return;
      const result = this.ledger.accept(fact);
      if (result.duplicate) return;
      if (result.turn?.conflict) this.unknownThreads.add(threadId);
      this.record(fact); if (result.turn) this.emit('turn', fact);
    } catch (error) {
      this.persistenceError = new AdapterError('OBSERVATION_FAILED', 'Native event projection failed; new mutations disabled', { causeCode: error.code });
      this.emit('persistence_failed', this.persistenceError.toJSON());
    }
  }
  onLateResponse(receipt) {
    const action = this.requests.get(receipt.requestId); if (!action) return;
    // Preserve late receipt identity for recovery; never replay the original request.
    this.record(this.fact('late_acknowledgement', { ...action,
      nativeResult: this.ackProjection(action.method, receipt.message.result),
      rpcError: receipt.message.error ? { code: receipt.message.error.code } : null,
      noAutomaticRetry: true }));
  }
  async close() {
    if (this.closed) return this.closeReceipt;
    this.emit('closing'); this.connected = false; this.closed = true;
    const transport = await this.transport.close();
    const fact = this.fact('runtime_closed', { transport, bindingThreadIds: [...this.owned.keys()],
      meaning: 'Own transport cleanup only; active turns need native terminal/readback, child process cleanup not independently verified' });
    let persisted;
    try { persisted = await this.record(fact, !!this.factSink); await this.flush(); }
    catch (error) { persisted = { persisted: false, error: error.toJSON?.() ?? { code: 'PERSISTENCE_FAILED' } }; }
    this.closeReceipt = { hostId: this.hostId, runtimeId: this.runtimeId, transport,
      fact: copy(fact),
      closedFactId: persisted?.persisted ? persisted.eventId : null, persistence: persisted,
      bindings: [...this.owned.values()].map(binding => ({ hostId: this.hostId, runtimeId: this.runtimeId,
        threadId: binding.threadId, terminalTurnIds: [...this.ledger.turns.values()].filter(t => t.threadId === binding.threadId && t.owned && t.terminal && !t.conflict).map(t => t.turnId) })) };
    this.removeAllListeners(); return this.closeReceipt;
  }
}
