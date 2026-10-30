// Public, single-invocation app-server JSONL transport. No daemon/socket/private DB.
// Extracted from native-probe/probe.mjs, with bounded buffers and lifecycle cleanup.
import { spawn, spawnSync } from 'node:child_process';
import { EventEmitter } from 'node:events';

export class AdapterError extends Error {
  constructor(code, message, facts = {}) {
    super(message); this.name = 'AdapterError'; this.code = code; this.facts = facts;
  }
  toJSON() { return { code: this.code, message: this.message, ...this.facts }; }
}
export const stamp = () => new Date().toISOString();
export const bounded = (value, limit = 1024) => String(value ?? '').slice(0, limit);

export function cliVersion({ command = 'codex', cwd, timeoutMs = 10000, env } = {}) {
  const result = spawnSync(command, ['--version'], { cwd, windowsHide: true, shell: false,
    encoding: 'utf8', timeout: timeoutMs, maxBuffer: 16384, ...(env !== undefined ? { env } : {}) });
  const text = bounded(result.stdout, 1024).trim();
  return { observedAt: stamp(), command, platform: process.platform, node: process.version,
    available: !result.error && result.status === 0, exitCode: result.status,
    version: text.match(/(?:codex(?:-cli)?\s+)?(\d+\.\d+\.\d+[^\s]*)/i)?.[1] ?? null,
    output: text, error: result.error ? { code: result.error.code ?? 'VERSION_FAILED',
      message: bounded(result.error.message) } : (result.status ? { code: 'VERSION_FAILED' } : null) };
}

export class StdioTransport extends EventEmitter {
  constructor({ command = 'codex', cwd, disabledFeatures = [], requestTimeoutMs = 20000,
    maxLineBytes = 8 * 1024 * 1024, maxPending = 64, maxDiagnostics = 128,
    spawnProcess = spawn } = {}) {
    super();
    if (!cwd || !Array.isArray(disabledFeatures) || disabledFeatures.some(x => !/^[a-z0-9_]+$/.test(x)))
      throw new AdapterError('INVALID', 'Explicit process cwd and feature names are required');
    this.config = { command, cwd, disabledFeatures: [...new Set(['daemon_auto_start', ...disabledFeatures])] };
    this.requestTimeoutMs = requestTimeoutMs; this.maxLineBytes = maxLineBytes;
    this.maxPending = maxPending; this.maxDiagnostics = maxDiagnostics;
    this.spawnProcess = spawnProcess; this.nextId = 1; this.pending = new Map();
    this.expired = new Map(); this.diagnostics = []; this.closed = false; this.closing = false;
    this.buffer = ''; this.stderrBytes = 0; this.proc = null;
  }
  diagnostic(kind, facts = {}) {
    const event = { kind, observedAt: stamp(), ...facts };
    this.diagnostics.push(event);
    if (this.diagnostics.length > this.maxDiagnostics) this.diagnostics.shift();
    this.emit('diagnostic', event);
  }
  launchArgs() { return ['app-server', '--stdio', ...this.config.disabledFeatures.flatMap(x => ['--disable', x])]; }
  open() {
    if (this.proc || this.closed) throw new AdapterError('CLOSED', 'Transport cannot be opened twice');
    const args = this.launchArgs();
    const proc = this.spawnProcess(this.config.command, args,
      { cwd: this.config.cwd, windowsHide: true, shell: false, stdio: ['pipe', 'pipe', 'pipe'] });
    this.proc = proc;
    this.exitPromise = new Promise(resolve => { this.resolveExit = resolve; });
    this.diagnostic('process_start', { command: this.config.command, args, cwd: this.config.cwd,
      pid: proc.pid, scope: 'own_single_stdio_process' });
    proc.stdout.setEncoding('utf8');
    proc.stdout.on('data', chunk => {
      this.buffer += chunk;
      let index;
      while ((index = this.buffer.indexOf('\n')) >= 0) {
        const line = this.buffer.slice(0, index); this.buffer = this.buffer.slice(index + 1);
        if (Buffer.byteLength(line) > this.maxLineBytes) { this.protocolFailure('oversized JSONL frame'); return; }
        if (line.trim()) this.receive(line);
      }
      if (Buffer.byteLength(this.buffer) > this.maxLineBytes) this.protocolFailure('oversized unfinished JSONL frame');
    });
    // stderr may contain private configuration text. Keep only its byte count.
    proc.stderr.on('data', chunk => { this.stderrBytes += chunk.length; });
    proc.stdin.on('error', error => this.failPending(error.code ?? 'STDIN_ERROR'));
    proc.on('error', error => {
      this.diagnostic('process_error', { code: error.code ?? 'PROCESS_ERROR' });
      this.closed = true; this.failPending('PROCESS_ERROR'); this.resolveExit?.();
      this.emit('disconnect', { reason: 'PROCESS_ERROR', observedAt: stamp() });
    });
    proc.on('exit', (code, signal) => {
      this.closed = true; this.buffer = ''; this.failPending('PROCESS_EXIT');
      this.diagnostic('process_exit', { code, signal, stderrBytes: this.stderrBytes });
      this.resolveExit?.(); this.emit('disconnect', { reason: 'PROCESS_EXIT', code, signal, observedAt: stamp() });
    });
    return this;
  }
  protocolFailure(reason) {
    this.buffer = ''; this.diagnostic('protocol_error', { reason });
    this.failPending('PROTOCOL_ERROR'); this.proc?.kill();
  }
  receive(line) {
    let message;
    try { message = JSON.parse(line); }
    catch { this.protocolFailure('invalid JSONL frame'); return; }
    if (!message || typeof message !== 'object') { this.protocolFailure('invalid JSONRPC envelope'); return; }
    if (message.id !== undefined && !message.method) {
      const key = String(message.id); const pending = this.pending.get(key);
      if (!pending) {
        const expired = this.expired.get(key);
        this.diagnostic('late_or_unmatched_response', { requestId: message.id, method: expired?.method });
        if (expired) this.emit('late_response', { requestId: message.id, method: expired.method,
          message, observedAt: stamp() });
        return;
      }
      this.pending.delete(key); clearTimeout(pending.timer);
      if (message.error) pending.reject(new AdapterError('REJECTED', bounded(message.error.message),
        { requestId: message.id, method: pending.method, rpcCode: message.error.code, mayHaveBeenSent: true }));
      else pending.resolve({ requestId: message.id, result: message.result, acknowledgedAt: stamp() });
      return;
    }
    if (typeof message.method !== 'string') { this.protocolFailure('missing method'); return; }
    if (message.id !== undefined) {
      // This adapter does not approve tool/permission/elicitation requests.
      this.write({ id: message.id, error: { code: -32601, message: 'Unsupported client request; no approval granted' } });
      this.diagnostic('server_request_rejected', { method: message.method });
      this.emit('server_request_rejected', { method: message.method, observedAt: stamp() });
    } else this.emit('notification', message);
  }
  write(message) {
    if (this.closed || this.closing || !this.proc || !this.proc.stdin.writable)
      throw new AdapterError('CLOSED', 'Transport is closed', { mayHaveBeenSent: false });
    this.proc.stdin.write(JSON.stringify(message) + '\n');
  }
  notify(method, params) { this.write({ method, params }); }
  async request(method, params, { timeoutMs = this.requestTimeoutMs, beforeSend } = {}) {
    if (this.closed || this.closing || !this.proc) throw new AdapterError('CLOSED', 'Transport is closed', { mayHaveBeenSent: false });
    if (this.pending.size >= this.maxPending) throw new AdapterError('BUSY', 'Pending request bound reached', { mayHaveBeenSent: false });
    const requestId = this.nextId++;
    // beforeSend is awaited: durable intent and current guard must finish before any bytes leave.
    await beforeSend?.({ requestId, method });
    if (this.closed || this.closing) throw new AdapterError('CLOSED', 'Transport closed before write', { requestId, mayHaveBeenSent: false });
    if (this.pending.size >= this.maxPending) throw new AdapterError('BUSY', 'Pending request bound reached before write', { requestId, mayHaveBeenSent: false });
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(String(requestId));
        this.expired.set(String(requestId), { method });
        if (this.expired.size > this.maxPending) this.expired.delete(this.expired.keys().next().value);
        this.diagnostic('request_unknown', { requestId, method, reason: 'response_timeout', noAutomaticRetry: true });
        reject(new AdapterError('UNKNOWN', 'Response timed out; reconcile exact target before any new action',
          { requestId, method, mayHaveBeenSent: true, noAutomaticRetry: true }));
      }, timeoutMs);
      this.pending.set(String(requestId), { resolve, reject, timer, method });
      try { this.write({ id: requestId, method, params }); }
      catch (error) { clearTimeout(timer); this.pending.delete(String(requestId)); reject(error); }
    });
  }
  failPending(reason) {
    for (const [requestId, pending] of this.pending) {
      clearTimeout(pending.timer); this.expired.set(requestId, { method: pending.method });
      pending.reject(new AdapterError('UNKNOWN', 'Connection lost; exact target needs reconciliation',
        { requestId: Number(requestId), method: pending.method, reason, mayHaveBeenSent: true, noAutomaticRetry: true }));
    }
    this.pending.clear();
    while (this.expired.size > this.maxPending) this.expired.delete(this.expired.keys().next().value);
  }
  async close({ graceMs = 1000, killWaitMs = 1000 } = {}) {
    if (!this.proc) { this.closed = true; return { closed: true, processStarted: false }; }
    this.closing = true; this.failPending('CLIENT_CLOSE');
    if (!this.closed) {
      this.proc.stdin.end();
      await waitBounded(this.exitPromise, graceMs);
      if (!this.closed) {
        this.diagnostic('own_process_terminate', { pid: this.proc.pid });
        this.proc.kill(); await waitBounded(this.exitPromise, killWaitMs);
      }
    }
    const receipt = { closed: this.closed, pid: this.proc.pid, stderrBytes: this.stderrBytes,
      pendingRequests: this.pending.size, observedAt: stamp(), scope: 'own_single_stdio_process' };
    if (!this.closed) receipt.error = 'UNKNOWN_PROCESS_EXIT';
    this.removeAllListeners(); return receipt;
  }
}

// Separate opt-in for the subscription RSI executor; never modifies global config.
// These are requested restrictions. Actual config, tool roster and command gates
// must be independently checked in the same runtime before any model turn.
export const FIXED_PROFILE_NAME = 'htam_rsi_eval';
export const PROFILED_PROCESS_PATH = 'C:\\Windows\\System32\\WindowsPowerShell\\v1.0;C:\\Windows\\System32;C:\\Windows';
export const PROFILED_REQUIRED_FEATURES = Object.freeze({ shell_tool: true, code_mode_host: true, unified_exec: true });
export const PROFILED_DISABLED_FEATURES = Object.freeze(['hooks', 'multi_agent', 'multi_agent_v2',
  'apps', 'plugins', 'remote_plugin', 'browser_use',
  'browser_use_external', 'computer_use', 'view_image', 'image_generation', 'goals',
  'sleep_tool', 'code_mode', 'daemon_auto_start', 'skill_search',
  'skill_mcp_dependency_install', 'memories']);
export const PROFILED_ENV_NAMES = Object.freeze(['SYSTEMROOT', 'WINDIR', 'PATH', 'PATHEXT',
  'USERPROFILE', 'HOMEDRIVE', 'HOMEPATH', 'TEMP', 'TMP', 'LOCALAPPDATA', 'APPDATA',
  'PROGRAMFILES', 'PROGRAMFILES(X86)', 'PROGRAMW6432', 'COMSPEC',
  'PROCESSOR_ARCHITECTURE', 'NUMBER_OF_PROCESSORS']);
export function profiledEnvironment() {
  const allow = new Set(PROFILED_ENV_NAMES);
  return { ...Object.fromEntries(Object.entries(process.env).filter(([key]) =>
    key.toUpperCase() !== 'PATH' && allow.has(key.toUpperCase()))), PATH: PROFILED_PROCESS_PATH };
}
export function profiledConfiguration({ permissionProfile = FIXED_PROFILE_NAME,
  windowsSandbox = 'mxc', mcpNames } = {}) {
  if (permissionProfile !== FIXED_PROFILE_NAME || windowsSandbox !== 'mxc' ||
      !Array.isArray(mcpNames) || mcpNames.length > 64 ||
      mcpNames.some(name => typeof name !== 'string' || !/^[A-Za-z0-9_-]{1,100}$/.test(name)))
    throw new AdapterError('INVALID_PROFILE', 'Fixed MXC profile and an explicit finite MCP name roster are required');
  const roster = [...new Set(mcpNames)].sort();
  const policy = { filesystem: { ':root': 'deny', ':minimal': 'read',
    ':workspace_roots': { '.': 'read', output: 'write' } }, network: { enabled: false } };
  const values = ['windows.sandbox="mxc"', `default_permissions="${FIXED_PROFILE_NAME}"`,
    `permissions.${FIXED_PROFILE_NAME}={filesystem={":root"="deny",":minimal"="read",":workspace_roots"={"."="read","output"="write"}},network={enabled=false}}`,
    'project_doc_max_bytes=0', 'web_search="disabled"',
    ...roster.map(name => `mcp_servers.${name}.enabled=false`),
    ...PROFILED_DISABLED_FEATURES.map(name => `features.${name}=false`),
    ...Object.entries(PROFILED_REQUIRED_FEATURES).map(([name, enabled]) => `features.${name}=${enabled}`)];
  return { permissionProfile, windowsSandbox, policy, mcpNames: roster, processPath: PROFILED_PROCESS_PATH,
    disabledFeatures: [...PROFILED_DISABLED_FEATURES], requiredFeatures: PROFILED_REQUIRED_FEATURES,
    args: values.flatMap(value => ['-c', value]),
    isolationClaim: 'fixed_requested_policy_requires_same_runtime_effective_config_roster_and_command_gate' };
}
export class ProfiledStdioTransport extends StdioTransport {
  constructor(options = {}) {
    const fields = ['command', 'cwd', 'permissionProfile', 'windowsSandbox', 'mcpNames',
      'requestTimeoutMs', 'maxLineBytes', 'maxPending', 'maxDiagnostics', 'spawnProcess'];
    if (Object.keys(options).some(key => !fields.includes(key)))
      throw new AdapterError('INVALID_PROFILE', 'Arbitrary config, environment and feature overrides are not accepted');
    const { permissionProfile, windowsSandbox, mcpNames, spawnProcess = spawn, ...base } = options;
    if (Object.hasOwn(base, 'env') || Object.hasOwn(base, 'processEnv') || Object.hasOwn(base, 'disabledFeatures') ||
        Object.hasOwn(base, 'config') || Object.hasOwn(base, 'args'))
      throw new AdapterError('INVALID_PROFILE', 'Arbitrary config, environment and feature overrides are not accepted');
    const configuration = profiledConfiguration({ permissionProfile, windowsSandbox, mcpNames });
    const environment = profiledEnvironment();
    super({ ...base, disabledFeatures: configuration.disabledFeatures,
      spawnProcess: (command, args, spawnOptions) => spawnProcess(command, args, { ...spawnOptions, env: environment }) });
    this.config.requiredFeatures = PROFILED_REQUIRED_FEATURES;
    this.config.processPath = configuration.processPath;
    this.profileConfiguration = configuration; this.customSpawnForTesting = spawnProcess !== spawn;
  }
  launchArgs() { return [...super.launchArgs(), ...this.profileConfiguration.args]; }
  open() {
    if (process.platform !== 'win32' && !this.customSpawnForTesting)
      throw new AdapterError('UNSUPPORTED_HOST', 'The MXC executor is pinned to Windows; no host fallback');
    return super.open();
  }
}

async function waitBounded(promise, ms) {
  let timer;
  try { await Promise.race([promise, new Promise(resolve => { timer = setTimeout(resolve, ms); })]); }
  finally { clearTimeout(timer); }
}
