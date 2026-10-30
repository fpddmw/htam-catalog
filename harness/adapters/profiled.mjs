// Experimental named-permission API, explicitly separated from the legacy driver.
// Permission-profile ACK identifies configuration provenance, not complete tool isolation.
import path from 'node:path';
import { CodexAdapter } from './codex.mjs';
import { AdapterError, ProfiledStdioTransport, FIXED_PROFILE_NAME, cliVersion, profiledEnvironment } from './transport.mjs';
import { fingerprint, terminalStatus } from './events.mjs';
export { ProfiledStdioTransport, FIXED_PROFILE_NAME, PROFILED_DISABLED_FEATURES,
  PROFILED_REQUIRED_FEATURES, PROFILED_PROCESS_PATH, profiledConfiguration } from './transport.mjs';

const clone = value => JSON.parse(JSON.stringify(value));
const key = value => path.win32.normalize(value).toLowerCase();
const exactId = (value, name) => {
  if (typeof value !== 'string' || !/^[A-Za-z0-9_-]{1,160}$/.test(value))
    throw new AdapterError('INVALID', `An exact ${name} is required`);
  return value;
};
function profileProjection(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
  return { id: typeof value.id === 'string' ? value.id : null,
    extends: value.extends === undefined ? null : clone(value.extends),
    unknownFields: Object.keys(value).filter(k => !['id', 'extends'].includes(k)).sort() };
}
function rootsMatch(actual, expected) {
  return Array.isArray(actual) && actual.length === 1 && typeof actual[0] === 'string' &&
    key(actual[0]) === key(expected[0]);
}
export class ProfiledCodexAdapter extends CodexAdapter {
  constructor(options = {}) {
    const { mcpNames, permissionProfile, windowsSandbox, ...base } = options;
    super({ ...base, transport: base.transport ?? new ProfiledStdioTransport({ command: base.command,
      cwd: base.processCwd, mcpNames, permissionProfile, windowsSandbox }) });
  }
  initializeParams() { return { ...super.initializeParams(), capabilities: { experimentalApi: true } }; }
  async connect() {
    this.versionEvidence ??= cliVersion({ command: this.command, cwd: this.processCwd, env: profiledEnvironment() });
    return super.connect();
  }
  capabilities() {
    return { ...super.capabilities(), permissionMode: 'experimental_named_profile',
      profile: FIXED_PROFILE_NAME, toolIsolation: 'unverified_by_adapter_requires_independent_same_runtime_gate',
      configurationRequested: this.transport.profileConfiguration ?? null };
  }
  settings(value) {
    const fields = ['model', 'reasoningEffort', 'cwd', 'permissionProfile', 'runtimeWorkspaceRoots', 'approvalPolicy'];
    if (!value || Object.keys(value).some(k => !fields.includes(k)) || fields.some(k => value[k] === undefined) ||
        typeof value.model !== 'string' || !value.model || typeof value.reasoningEffort !== 'string' || !value.reasoningEffort ||
        value.permissionProfile !== FIXED_PROFILE_NAME || value.approvalPolicy !== 'never' ||
        typeof value.cwd !== 'string' || !path.win32.isAbsolute(value.cwd) ||
        !Array.isArray(value.runtimeWorkspaceRoots) || !rootsMatch(value.runtimeWorkspaceRoots, [value.cwd]))
      throw new AdapterError('INVALID_PROFILE', 'Explicit model/effort/Windows cwd/fixed profile/single cwd root/never are required; legacy sandbox fields are mutually exclusive');
    if (!this.allowedCwds.has(key(value.cwd))) throw new AdapterError('OUT_OF_SCOPE', 'Profile cwd is outside the exact allowed set');
    return clone(value);
  }
  verifySettings(response, settings) {
    const profile = profileProjection(response.activePermissionProfile);
    return response.model === settings.model && response.reasoningEffort === settings.reasoningEffort &&
      typeof response.cwd === 'string' && key(response.cwd) === key(settings.cwd) &&
      response.approvalPolicy === 'never' && profile?.id === FIXED_PROFILE_NAME &&
      profile.extends === null && profile.unknownFields.length === 0 &&
      rootsMatch(response.runtimeWorkspaceRoots, settings.runtimeWorkspaceRoots);
  }
  ackProjection(method, result) {
    if (!method.startsWith('thread/')) return super.ackProjection(method, result);
    return { threadId: result?.thread?.id, model: result?.model, reasoningEffort: result?.reasoningEffort,
      cwd: result?.cwd, approvalPolicy: result?.approvalPolicy,
      activePermissionProfile: profileProjection(result?.activePermissionProfile),
      runtimeWorkspaceRoots: Array.isArray(result?.runtimeWorkspaceRoots) ? clone(result.runtimeWorkspaceRoots) : null,
      permissionMeaning: 'Named profile identity and roots only; same-runtime effective policy/tool gate independently required',
      persistence: 'identity_acknowledged_persistence_unconfirmed' };
  }
  bindResponse(response, settings, threadId) {
    const result = super.bindResponse(response, settings, threadId);
    result.permissionVerification = { namedProfileIdentityVerified: true, runtimeWorkspaceRootsVerified: true,
      policySemanticsVerified: false, toolIsolationVerified: false,
      requiredEvidence: 'same_runtime_config_profile_roster_and_command_gate' };
    return result;
  }
  threadParams(settings) {
    return { model: settings.model, cwd: settings.cwd, permissions: settings.permissionProfile,
      runtimeWorkspaceRoots: settings.runtimeWorkspaceRoots, approvalPolicy: settings.approvalPolicy,
      config: { model_reasoning_effort: settings.reasoningEffort } };
  }
  async createThread({ settings: desired, intent, baseInstructions, developerInstructions, ephemeral = false } = {}) {
    const settings = this.settings(desired);
    const response = await this.perform('thread/start', { ...this.threadParams(settings), ephemeral,
      ...(baseInstructions !== undefined ? { baseInstructions } : {}),
      ...(developerInstructions !== undefined ? { developerInstructions } : {}) }, intent, { requestedSettings: settings });
    return this.bindResponse(response, settings);
  }
  async resumeThread({ threadId, settings: desired, intent, priorOwnership } = {}) {
    this.scope(threadId); const settings = this.settings(desired);
    if (!priorOwnership || priorOwnership.threadId !== threadId || priorOwnership.hostId !== this.hostId ||
        !priorOwnership.runtimeId || priorOwnership.runtimeId === this.runtimeId ||
        !priorOwnership.closedFactId || !priorOwnership.terminalTurnId)
      throw new AdapterError('UNSUPPORTED', 'Resume requires accurately closed prior owned runtime');
    const check = await this.reconcile({ threadId, turnId: priorOwnership.terminalTurnId });
    if (check.state !== 'TERMINAL' || check.latestObservedTurnId !== priorOwnership.terminalTurnId)
      throw new AdapterError('UNKNOWN', 'Latest prior terminal ownership could not be reconciled');
    const response = await this.perform('thread/resume', { threadId, excludeTurns: true, ...this.threadParams(settings) },
      intent, { requestedSettings: settings, priorOwnership: clone(priorOwnership) });
    if (response.result?.thread?.turns?.some(t => !terminalStatus(t.status)))
      throw new AdapterError('UNKNOWN', 'Resume reported activity; no further control permitted');
    return this.bindResponse(response, settings, threadId);
  }
  async startTurn({ threadId, settings: desired, input, clientUserMessageId, intent, outputSchema } = {}) {
    this.ownedThread(threadId); const settings = this.settings(desired); exactId(clientUserMessageId, 'clientUserMessageId');
    if (this.unknownThreads.has(threadId) || [...this.ledger.turns.values()].some(t => t.threadId === threadId &&
        (t.conflict || (t.owned && !t.terminal)))) throw new AdapterError('UNKNOWN', 'Prior activity needs exact reconciliation');
    if (outputSchema !== undefined && (!outputSchema || typeof outputSchema !== 'object' || Array.isArray(outputSchema) ||
        Buffer.byteLength(JSON.stringify(outputSchema)) > 65536))
      throw new AdapterError('INVALID', 'outputSchema must be a bounded explicit JSON object');
    const response = await this.perform('turn/start', { threadId, model: settings.model,
      effort: settings.reasoningEffort, cwd: settings.cwd, permissions: settings.permissionProfile,
      runtimeWorkspaceRoots: settings.runtimeWorkspaceRoots, approvalPolicy: settings.approvalPolicy,
      clientUserMessageId, input: this.input(input), ...(outputSchema !== undefined ? { outputSchema: clone(outputSchema) } : {}) },
    intent, { requestedSettings: settings, ...(outputSchema !== undefined ? { outputSchemaSha256: fingerprint(outputSchema) } : {}) });
    const turnId = exactId(response.result?.turn?.id, 'turnId');
    this.ledger.acknowledge(threadId, turnId, { intentId: intent.intentId, clientUserMessageId, requestedSettings: settings });
    this.emit('turn', { threadId, turnId });
    return { ...this.ledger.get(threadId, turnId), action: response.action, entry: this.entry(threadId),
      executionSettings: { requested: settings, confirmedByTurnResponse: false,
        basis: 'Explicit named-profile overrides; initial thread profile/roots separately confirmed, actual isolation gate independent' } };
  }
}
