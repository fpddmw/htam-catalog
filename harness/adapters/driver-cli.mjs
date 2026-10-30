// Explicit bounded entry point. No background service, listening port or task creation.
import { readFile, stat } from 'node:fs/promises';
import { OwnedStdioDriver, loadManifest, readRunStatus, submitControl, reconcileRun } from './driver.mjs';

async function main() {
  const [operation, ...args] = process.argv.slice(2), options = {};
  if (!['run', 'status', 'control', 'reconcile', 'inspect'].includes(operation)) throw new Error('Use run|status|control|reconcile|inspect');
  for (let i = 0; i < args.length; i += 2) {
    const key = args[i];
    if (!['--manifest', '--manifest-sha256', '--slot', '--payload'].includes(key) || options[key] !== undefined || !args[i + 1])
      throw new Error('Explicit unique manifest/hash and, for control only, slot/payload required');
    options[key] = args[i + 1];
  }
  if (operation !== 'control' && (options['--slot'] || options['--payload'])) throw new Error('Control options are not accepted by another operation');
  const loaded = await loadManifest(options['--manifest'], options['--manifest-sha256']);
  if (operation === 'inspect') return { driver: 'one_shot_owned_stdio', manifestSha256: loaded.manifestSha256, manifest: loaded.manifest,
    nativeProcessOpened: false, authorization: 'Manifest freezes parameters; actual State prepare/guard remain authoritative',
    isolation: 'not_claimed', automaticRestartOrResend: false };
  if (operation === 'status') return readRunStatus(loaded);
  if (operation === 'reconcile') return reconcileRun(loaded);
  if (operation === 'run') {
    const result = await new OwnedStdioDriver(loaded).run();
    if (result.failure) process.exitCode = 2;
    return result;
  }
  if (!options['--slot'] || !options['--payload']) throw new Error('Control requires exact slot and JSON payload file');
  if ((await stat(options['--payload'])).size > 49152) throw new Error('Control payload exceeds 48KiB');
  return submitControl({ ...loaded, slotId: options['--slot'], payload: JSON.parse(await readFile(options['--payload'], 'utf8')) });
}
try { process.stdout.write(JSON.stringify(await main(), null, 2) + '\n'); }
catch (error) {
  process.stdout.write(JSON.stringify({ error: error.toJSON?.() ?? { code: error.code ?? 'DRIVER_FAILED', message: String(error.message).slice(0, 1024) },
    noAutomaticRetry: true, recovery: 'Read status and exact durable/native identities before another action' }, null, 2) + '\n');
  process.exitCode = 1;
}
