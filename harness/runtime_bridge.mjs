/** Commit guard/facts through the same State CLI; no model or native process is launched here. */
import { spawn } from 'node:child_process';

export function stateCallbacks({ command, args = [], root, store, timeoutMs = 30000, maxBytes = 8 * 1024 * 1024 } = {}) {
  if (typeof command !== 'string' || !command || !Array.isArray(args) || !args.every(x => typeof x === 'string') ||
      typeof root !== 'string' || !root || typeof store !== 'string' || !store)
    throw new Error('Explicit interpreter/CLI argv, root and isolated or authorized store are required');
  const call = (operation, options = {}) => new Promise((resolve, reject) => {
    const cliArgs = options.args ?? [], hasBody = Object.hasOwn(options, 'body');
    if (typeof operation !== 'string' || !/^[a-z][a-z0-9-]*$/.test(operation) || !Array.isArray(cliArgs) ||
        !cliArgs.every(value => typeof value === 'string') || cliArgs.some(value => ['--root', '--store'].includes(value.split('=')[0])) ||
        (hasBody && cliArgs.some(value => value.split('=')[0] === '--file')))
      return reject(new Error('State call requires one command, explicit argv and the configured root/store scope'));
    const input = hasBody ? JSON.stringify(options.body) : '';
    if (typeof input !== 'string' || Buffer.byteLength(input) + Buffer.byteLength(JSON.stringify(cliArgs)) > maxBytes)
      return reject(new Error('State request exceeds bridge bound or has no JSON body'));
    const child = spawn(command, [...args, '--root', root, '--store', store, operation, ...cliArgs, ...(hasBody ? ['--file', '-'] : [])],
      { stdio: ['pipe', 'pipe', 'pipe'], windowsHide: true, shell: false });
    let output = '', error = '', settled = false;
    const finish = (failure, result) => { if (settled) return; settled = true; clearTimeout(timer); failure ? reject(failure) : resolve(result); };
    const timer = setTimeout(() => { child.kill(); finish(new Error('State callback timed out; send remains unknown, never retry automatically')); }, timeoutMs);
    child.on('error', failure => finish(failure));
    child.stdin.on('error', failure => finish(failure));
    child.stdout.setEncoding('utf8'); child.stderr.setEncoding('utf8');
    child.stdout.on('data', chunk => { output += chunk; if (Buffer.byteLength(output) > maxBytes) { child.kill(); finish(new Error('State response exceeds bridge bound')); } });
    child.stderr.on('data', chunk => { if (error.length < 8192) error += chunk.toString().slice(0, 8192 - error.length); });
    child.on('close', code => {
      if (code !== 0) return finish(new Error('State callback refused or failed: ' + error.trim()));
      try { finish(null, JSON.parse(output)); } catch { finish(new Error('State callback returned invalid JSON')); }
    });
    child.stdin.end(input);
  });
  return { call, guard: request => call('runtime-guard', { body: request }), factSink: fact => call('runtime-fact', { body: fact }) };
}
