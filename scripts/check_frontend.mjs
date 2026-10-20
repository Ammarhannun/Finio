// Syntax and local-link checks; no browser or credentials required.
import { readFile, readdir, access, mkdtemp, rm } from 'node:fs/promises';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { SourceTextModule, Script } from 'node:vm';
import { tmpdir } from 'node:os';
import { spawnSync } from 'node:child_process';
import assert from 'node:assert/strict';
const root = resolve(dirname(fileURLToPath(import.meta.url)), '../frontend');
let scripts = 0;
for (const file of await readdir(root)) {
  if (!/\.(html|js|mjs)$/.test(file)) continue;
  const source = await readFile(join(root, file), 'utf8');
  if (/\.m?js$/.test(file)) { new SourceTextModule(source, { identifier: file }); scripts++; continue; }
  for (const match of source.matchAll(/<script\b([^>]*)>([\s\S]*?)<\/script>/gi)) {
    if (!match[2].trim()) continue;
    if (/type=["']module["']/.test(match[1])) new SourceTextModule(match[2], { identifier: file });
    else new Script(match[2], { filename: file });
    scripts++;
  }
  for (const match of source.matchAll(/(?:href|src)=["']([^"']+)["']/g)) {
    const path = match[1].split(/[?#]/)[0];
    if (!path || /^(?:https?:|data:|mailto:|tel:|\/)/.test(path) || path.includes('${')) continue;
    await access(join(root, path));
  }
}
console.log(`PASS: ${scripts} JavaScript modules/scripts and local HTML links`);
const temporary = await mkdtemp(join(tmpdir(), 'finio-config-test-'));
try {
  const output = join(temporary, 'config.mjs');
  const env = { ...process.env, FINIO_API_BASE_URL: 'https://api.example.invalid',
    FINIO_PUBLIC_SUPABASE_URL: 'https://db.example.invalid', FINIO_PUBLIC_SUPABASE_KEY: 'sb_publishable_synthetic' };
  const run = variables => spawnSync(process.execPath, [join(root, 'build-config.mjs'), output],
    { env: variables, encoding: 'utf8' });
  assert.equal(run(env).status, 0);
  assert.match(await readFile(output, 'utf8'), /api.example.invalid/);
  for (const vars of [{ FINIO_API_BASE_URL: '' }, { FINIO_API_BASE_URL: 'http://insecure.invalid' },
    { FINIO_PUBLIC_SUPABASE_KEY: 'sb_secret_test' }]) {
    assert.notEqual(run({ ...env, ...vars }).status, 0);
  }
  console.log('PASS: frontend production config accepts public values and rejects missing/insecure/secret settings');
} finally {
  await rm(temporary, { recursive: true });
}
