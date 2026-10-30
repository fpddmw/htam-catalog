// Match the public verifier's stable-JSON hash without importing private CLI code.
import { createHash } from 'node:crypto';
let input = '';
for await (const chunk of process.stdin) input += chunk;
const stable = (v) => Array.isArray(v) ? v.map(stable) : v && typeof v === 'object'
  ? Object.fromEntries(Object.keys(v).sort().map(k => [k, stable(v[k])])) : v;
process.stdout.write(JSON.stringify(JSON.parse(input).map(v =>
  createHash('sha256').update(JSON.stringify(stable(v))).digest('hex'))));
