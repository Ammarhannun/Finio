import { readFileSync, writeFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

const env = process.env;
const api = env.FINIO_API_BASE_URL;
const supabase = env.FINIO_PUBLIC_SUPABASE_URL;
const key = env.FINIO_PUBLIC_SUPABASE_KEY;
for (const [name, value] of Object.entries({ FINIO_API_BASE_URL: api,
  FINIO_PUBLIC_SUPABASE_URL: supabase, FINIO_PUBLIC_SUPABASE_KEY: key })) {
  if (!value) throw new Error(`Set ${name} in the frontend deployment environment.`);
}
for (const value of [api, supabase]) {
  const url = new URL(value);
  if (url.protocol !== 'https:' || url.username || url.password || url.search || url.hash || url.pathname !== '/') {
    throw new Error('Public service URLs must be HTTPS origins without paths or credentials.');
  }
}
let publicKey = key.startsWith('sb_publishable_');
if (key.startsWith('eyJ')) {
  try { publicKey = JSON.parse(Buffer.from(key.split('.')[1], 'base64url').toString()).role === 'anon'; }
  catch { publicKey = false; }
}
if (!publicKey) throw new Error('Use only a Supabase publishable/anon key for the frontend.');
// Keep generated URLs in sync with the deployed headers. A configuration the
// browser blocks must fail the build instead of producing a broken site.
const deployment = JSON.parse(readFileSync(new URL('./vercel.json', import.meta.url), 'utf8'));
const policy = deployment.headers?.flatMap(entry => entry.headers)
  .find(header => header.key === 'Content-Security-Policy')?.value || '';
const sources = policy.match(/(?:^|;)\s*connect-src\s+([^;]+)/)?.[1].trim().split(/\s+/) || [];
function allowedOrigin(value) {
  const origin = new URL(value);
  return sources.some(source => {
    if (source === origin.origin) return true;
    if (!source.startsWith('https://*.')) return false;
    const allowed = new URL(source.replace('*.', 'wildcard.'));
    const suffix = allowed.hostname.slice('wildcard'.length);
    return origin.protocol === allowed.protocol && origin.port === allowed.port
      && origin.hostname.endsWith(suffix);
  });
}
for (const origin of [api, supabase]) {
  if (!allowedOrigin(origin)) {
    throw new Error(`Add ${new URL(origin).origin} to connect-src in frontend/vercel.json before building.`);
  }
}
const values = { SUPABASE_URL: supabase.replace(/\/$/, ''), SUPABASE_ANON_KEY: key,
  API_BASE_URL: api.replace(/\/$/, ''), CURRENCY: env.FINIO_CURRENCY || 'AUD', LOCALE: env.FINIO_LOCALE || 'en-AU' };
const output = process.argv[2] || fileURLToPath(new URL('./config.js', import.meta.url));
writeFileSync(output, Object.entries(values).map(([name, value]) => `export const ${name} = ${JSON.stringify(value)};`).join('\n') + '\n');
console.log('Generated public frontend configuration.');
