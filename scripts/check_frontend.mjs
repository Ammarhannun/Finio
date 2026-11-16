// Syntax and local-link checks; no browser or credentials required.
import { readFile, readdir, access, mkdtemp, rm, writeFile } from 'node:fs/promises';
import { dirname, join, resolve, sep } from 'node:path';
import { fileURLToPath } from 'node:url';
import { SourceTextModule, Script } from 'node:vm';
import { tmpdir } from 'node:os';
import { spawnSync } from 'node:child_process';
import assert from 'node:assert/strict';
const root = resolve(dirname(fileURLToPath(import.meta.url)), '../frontend');
let scripts = 0;
async function checkImports(source, file) {
  const module = new SourceTextModule(source, { identifier: file });
  for (const specifier of module.dependencySpecifiers) {
    if (file === 'build-config.mjs' && specifier.startsWith('node:')) continue;
    if (/^https:\/\//.test(specifier)) continue;
    assert.ok(specifier.startsWith('.') || specifier.startsWith('/'), `${file}: browser import needs a URL: ${specifier}`);
    const path = specifier.split(/[?#]/)[0];
    const target = path.startsWith('/') ? resolve(root, '.' + path) : resolve(dirname(join(root, file)), path);
    assert.ok(target.startsWith(root + sep), `${file}: import escapes frontend: ${specifier}`);
    await access(target);
  }
}
for (const file of await readdir(root)) {
  if (!/\.(html|js|mjs)$/.test(file)) continue;
  const source = await readFile(join(root, file), 'utf8');
  if (/\.m?js$/.test(file)) { await checkImports(source, file); scripts++; continue; }
  for (const match of source.matchAll(/<script\b([^>]*)>([\s\S]*?)<\/script>/gi)) {
    if (!match[2].trim()) continue;
    if (/type=["']module["']/.test(match[1])) await checkImports(match[2], file);
    else new Script(match[2], { filename: file });
    scripts++;
  }
  for (const match of source.matchAll(/(?:href|src)=["']([^"']+)["']/g)) {
    const path = match[1].split(/[?#]/)[0];
    if (!path || /^(?:https?:|data:|mailto:|tel:|\/)/.test(path) || path.includes('${')) continue;
    await access(join(root, path));
  }
}
console.log(`PASS: ${scripts} JavaScript modules/scripts, local imports and HTML links`);
const temporary = await mkdtemp(join(tmpdir(), 'finio-config-test-'));
try {
  const output = join(temporary, 'config.mjs');
  const env = { ...process.env, FINIO_API_BASE_URL: 'https://finio-test.up.railway.app',
    FINIO_PUBLIC_SUPABASE_URL: 'https://finio-test.supabase.co', FINIO_PUBLIC_SUPABASE_KEY: 'sb_publishable_synthetic' };
  const run = variables => spawnSync(process.execPath, [join(root, 'build-config.mjs'), output],
    { env: variables, encoding: 'utf8' });
  assert.equal(run(env).status, 0);
  assert.match(await readFile(output, 'utf8'), /finio-test.up.railway.app/);
  for (const vars of [{ FINIO_API_BASE_URL: '' }, { FINIO_API_BASE_URL: 'http://insecure.invalid' },
    { FINIO_PUBLIC_SUPABASE_KEY: 'sb_secret_test' },
    { FINIO_API_BASE_URL: 'https://api.example.invalid' },
    { FINIO_PUBLIC_SUPABASE_URL: 'https://db.example.invalid' },
    { FINIO_API_BASE_URL: 'https://up.railway.app.evil.invalid' }]) {
    assert.notEqual(run({ ...env, ...vars }).status, 0);
  }
  const custom = JSON.parse(await readFile(join(root, 'vercel.json'), 'utf8'));
  const csp = custom.headers.flatMap(entry => entry.headers).find(header => header.key === 'Content-Security-Policy');
  csp.value = csp.value.replace('connect-src ', 'connect-src https://api.example.invalid https://db.example.invalid ');
  const builder = join(temporary, 'build-config.mjs');
  await writeFile(builder, await readFile(join(root, 'build-config.mjs'), 'utf8'));
  await writeFile(join(temporary, 'vercel.json'), JSON.stringify(custom));
  assert.equal(spawnSync(process.execPath, [builder, output], { encoding: 'utf8', env: { ...env,
    FINIO_API_BASE_URL: 'https://api.example.invalid', FINIO_PUBLIC_SUPABASE_URL: 'https://db.example.invalid' } }).status, 0);
  console.log('PASS: production config rejects missing/insecure/secret values and origins blocked by CSP');
} finally {
  await rm(temporary, { recursive: true });
}

// ── Content-Security-Policy ────────────────────────────────────────────────
// Scripts are allowed by hash, never by 'unsafe-inline', so an edit to any
// inline <script> changes its hash and the browser silently refuses to run
// it — a break that only ever shows up in production. Recompute the hashes
// here so that drift fails this gate instead.
{
  const { createHash } = await import('node:crypto');
  const expected = [];
  for (const file of (await readdir(root)).sort()) {
    if (!file.endsWith('.html')) continue;
    const source = await readFile(join(root, file), 'utf8');
    for (const match of source.matchAll(/<script(?![^>]*\bsrc=)[^>]*>([\s\S]*?)<\/script>/gi)) {
      if (!match[1].trim()) continue;
      const hash = `sha256-${createHash('sha256').update(match[1]).digest('base64')}`;
      if (!expected.includes(hash)) expected.push(hash);
    }
  }

  const config = JSON.parse(await readFile(join(root, 'vercel.json'), 'utf8'));
  const header = config.headers
    .flatMap(entry => entry.headers)
    .find(entry => entry.key === 'Content-Security-Policy');
  assert.ok(header, 'vercel.json must set a Content-Security-Policy');
  const policy = header.value;

  const missing = expected.filter(hash => !policy.includes(hash));
  assert.deepEqual(missing, [], `inline scripts changed — add to script-src in frontend/vercel.json:\n${
    missing.map(hash => `  '${hash}'`).join('\n')}`);

  const stale = [...policy.matchAll(/'(sha256-[^']+)'/g)]
    .map(match => match[1])
    .filter(hash => !expected.includes(hash));
  assert.deepEqual(stale, [], `script-src has hashes matching no inline script:\n${
    stale.map(hash => `  '${hash}'`).join('\n')}`);

  // 'unsafe-inline' in script-src would defeat the hashes entirely. It stays
  // allowed for style-src only: a style= attribute cannot carry a hash.
  const scriptSrc = policy.match(/script-src ([^;]*)/)?.[1] ?? '';
  assert.ok(!scriptSrc.includes('unsafe-inline'), "script-src must not allow 'unsafe-inline'");
  assert.ok(!scriptSrc.includes('unsafe-eval'), "script-src must not allow 'unsafe-eval'");

  // Every external origin the pages actually reach must be in the policy, or
  // the resource is blocked at runtime.
  for (const [origin, directive] of [
    ['https://cdn.jsdelivr.net', 'script-src'],   // Chart.js
    ['https://esm.sh', 'script-src'],             // Supabase client
    ['https://fonts.googleapis.com', 'style-src'],
    ['https://fonts.gstatic.com', 'font-src'],
    ['https://*.supabase.co', 'connect-src'],
  ]) {
    const value = policy.match(new RegExp(`${directive} ([^;]*)`))?.[1] ?? '';
    assert.ok(value.includes(origin), `${directive} must allow ${origin}`);
  }

  for (const directive of ["frame-ancestors 'none'", "object-src 'none'", "base-uri 'self'"]) {
    assert.ok(policy.includes(directive), `CSP must include ${directive}`);
  }
  console.log(`PASS: CSP covers ${expected.length} inline scripts by hash, no unsafe-inline for scripts`);
}
