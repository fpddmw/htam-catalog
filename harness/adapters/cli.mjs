// Deliberately read-only. Mutation API is available only to a guard/sink integrator.
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { CodexAdapter } from './codex.mjs';
import { StdioTransport, AdapterError, cliVersion, stamp } from './transport.mjs';

export async function runReadOnly(argv) {
  const operation = argv[0]; const options = {};
  const names = new Set(['host', 'codex', 'process-cwd', 'thread', 'turn', 'client-message-id', 'max-pages', 'page-size', 'timeout-ms',
    'anchor-turn', 'anchor-item', 'anchor-content-sha256', 'max-item-pages', 'item-page-size', 'max-turns', 'max-items', 'max-messages',
    'max-text-bytes', 'max-total-text-bytes', 'max-requests', 'deadline-ms']);
  for (let i = 1; i < argv.length; i++) {
    const key = argv[i].replace(/^--/, '');
    if (key === 'connect' && argv[i] === '--connect') { options.connect = true; continue; }
    if (!argv[i].startsWith('--') || !names.has(key) || options[key] !== undefined || i + 1 >= argv.length)
      throw new AdapterError('INVALID', 'Unknown, repeated or incomplete read-only CLI option');
    options[key] = argv[++i];
  }
  if (!['version', 'capabilities', 'read', 'reconcile', 'timeline'].includes(operation) || !options.host)
    throw new AdapterError('INVALID', 'Use version|capabilities|read|reconcile|timeline with explicit --host');
  if (operation === 'timeline' && (!options.thread || ((!options['anchor-turn']) && (options['anchor-item'] || options['anchor-content-sha256']))))
    throw new AdapterError('INVALID', 'Timeline needs an exact thread and consistent anchor');
  const command = options.codex ?? 'codex'; const cwd = options['process-cwd'] ?? dirname(fileURLToPath(import.meta.url));
  const version = cliVersion({ command, cwd });
  if (operation === 'version') return { formatVersion: '1.0', operation, hostId: options.host, ...version };
  const timeoutMs = Number(options['timeout-ms'] ?? 15000);
  if (!Number.isInteger(timeoutMs) || timeoutMs < 1 || timeoutMs > 60000) throw new AdapterError('INVALID', 'timeout-ms must be 1..60000');
  const transport = new StdioTransport({ command, cwd, requestTimeoutMs: timeoutMs });
  const adapter = new CodexAdapter({ hostId: options.host, command, processCwd: cwd,
    allowedThreadIds: options.thread ? [options.thread] : [], transport, versionEvidence: version });
  let result;
  try {
    if (operation === 'capabilities' && !options.connect) result = adapter.capabilities();
    else {
      await adapter.connect();
      if (operation === 'capabilities') result = adapter.capabilities();
      else if (operation === 'read') result = await adapter.readThread(options.thread);
      else if (operation === 'timeline') result = await adapter.timeline({ threadId: options.thread,
        anchor: options['anchor-turn'] ? { turnId: options['anchor-turn'],
          ...(options['anchor-item'] ? { itemId: options['anchor-item'] } : {}),
          ...(options['anchor-content-sha256'] ? { contentSha256: options['anchor-content-sha256'] } : {}) } : null,
        maxPages: Number(options['max-pages'] ?? 3), pageSize: Number(options['page-size'] ?? 10),
        maxItemPages: Number(options['max-item-pages'] ?? 3), itemPageSize: Number(options['item-page-size'] ?? 20),
        maxTurns: Number(options['max-turns'] ?? 30), maxItems: Number(options['max-items'] ?? 200), maxMessages: Number(options['max-messages'] ?? 64),
        maxTextBytes: Number(options['max-text-bytes'] ?? 16384), maxTotalTextBytes: Number(options['max-total-text-bytes'] ?? 65536),
        maxRequests: Number(options['max-requests'] ?? 20), deadlineMs: Number(options['deadline-ms'] ?? 30000) });
      else result = await adapter.reconcile({ threadId: options.thread, turnId: options.turn,
        clientUserMessageId: options['client-message-id'], maxPages: Number(options['max-pages'] ?? 3),
        pageSize: Number(options['page-size'] ?? 20) });
    }
  } finally {
    const cleanup = await adapter.close();
    if (result) result.cleanup = cleanup;
  }
  return { formatVersion: '1.0', operation, hostId: options.host, observedAt: stamp(), result };
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try { console.log(JSON.stringify(await runReadOnly(process.argv.slice(2)))); }
  catch (error) {
    console.log(JSON.stringify({ formatVersion: '1.0', observedAt: stamp(),
      error: error.toJSON?.() ?? { code: 'ADAPTER_FAILURE', message: 'Read-only adapter invocation failed' } }));
    process.exitCode = 2;
  }
}
