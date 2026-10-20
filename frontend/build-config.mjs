import { writeFileSync } from 'node:fs';
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
const values = { SUPABASE_URL: supabase.replace(/\/$/, ''), SUPABASE_ANON_KEY: key,
  API_BASE_URL: api.replace(/\/$/, ''), CURRENCY: env.FINIO_CURRENCY || 'AUD', LOCALE: env.FINIO_LOCALE || 'en-AU' };
const output = process.argv[2] || fileURLToPath(new URL('./config.js', import.meta.url));
writeFileSync(output, Object.entries(values).map(([name, value]) => `export const ${name} = ${JSON.stringify(value)};`).join('\n') + '\n');
console.log('Generated public frontend configuration.');
