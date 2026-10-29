import { test } from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import { check } from './next-boundaries.mjs';

const SCRIPT = path.join(path.dirname(fileURLToPath(import.meta.url)), 'next-boundaries.mjs');

function tree(t, files) {
  const root = mkdtempSync(path.join(tmpdir(), 'next-boundaries-'));
  t.after(() => rmSync(root, { recursive: true, force: true }));
  for (const [file, text] of Object.entries(files)) {
    mkdirSync(path.dirname(path.join(root, file)), { recursive: true });
    writeFileSync(path.join(root, file), text);
  }
  return root;
}

const lines = (...parts) => parts.join('\n');
const TSCONFIG = JSON.stringify({
  compilerOptions: { paths: { '@/*': ['./*'] }, jsx: 'preserve' },
});

/** Server and client code on the right side of the boundary. */
const POSITIVE = {
  'tsconfig.json': TSCONFIG,
  'app/layout.tsx': lines(
    "import { cookies } from 'next/headers';",
    "import { Counter } from '@/components/counter';",
    "import { rows } from '@/lib/data.server';",
    "export const metadata = { title: 'x' };",
    "export const dynamic = 'force-dynamic';",
    'export const revalidate = 60;',
    'export const maxDuration = 30;',
    'export const dynamicParams = true;',
    "export const fetchCache = 'default-no-store';",
    'export default async function L({ children }) {',
    "  const theme = (await cookies()).get('t');",
    "  if (typeof window !== 'undefined') return null;",
    '  return <html><body data-theme={theme}><button type="submit">go</button><Counter rows={rows()} />{children}</body></html>;',
    '}',
  ),
  'components/counter.tsx': lines(
    "'use client';",
    "import Image from 'next/image';",
    "import { useState } from 'react';",
    "import type { Row } from '@/lib/data.server';",
    "import pic from './pic.png';",
    "import { save } from '@/app/actions';",
    'export function Counter({ rows, src }: { rows: Row[]; src: string }) {',
    '  const [n, setN] = useState(0);',
    '  const mode = process.env.NODE_ENV + process.env.NEXT_PUBLIC_MODE;',
    '  return (',
    '    <div onClick={() => { setN(n + 1); void save(); }} data-mode={mode}>',
    '      <Image src={pic} alt="" />',
    '      <picture><source srcSet="/a.webp" /><img src="/a.png" alt="" /></picture>',
    '      <img src={src} alt="" />',
    '      {rows.length}',
    '    </div>',
    '  );',
    '}',
  ),
  'components/pic.png': '',
  'app/actions.ts': lines(
    "'use server';",
    "import { headers } from 'next/headers';",
    'export async function save() { await headers(); }',
  ),
  'lib/data.server.ts': lines(
    "import 'server-only';",
    'export type Row = { id: string };',
    'export const rows = (): Row[] => [];',
  ),
  'app/opengraph-image.tsx':
    'export default function OG() { return <div><img src="/og.png" alt="" /></div>; }',
  'app/page.tsx': lines(
    '"use strict";',
    '"use client";',
    'export default function P() { return <p />; }',
  ),
};

test('positive tree: server and client code each use only the APIs of their side', (t) => {
  const root = tree(t, POSITIVE);
  assert.deepEqual(check({ root }), []);
});

/** Every rule broken once. */
const NEGATIVE = {
  'tsconfig.json': TSCONFIG,
  'app/layout.tsx': lines(
    "import { useState } from 'react';",
    "import { usePathname } from 'next/navigation';",
    "import dynamic from 'next/dynamic';",
    "import Head from 'next/head';",
    "import 'client-only';",
    "import { revalidate } from './helper';",
    "const Lazy = dynamic(() => import('./lazy'), { ssr: false });",
    'export default function L({ children }) {',
    '  const href = window.location.href;',
    '  return <html><body><button onClick={() => useState(0)} data-p={usePathname()}>{href}</button><Head /><Lazy />{children}{revalidate}</body></html>;',
    '}',
  ),
  'app/lazy.tsx': 'export default function Lazy() { return null; }',
  'app/helper.ts': 'export const revalidate = 10;',
  'app/client/page.tsx': lines(
    "'use client';",
    "import { cookies } from 'next/headers';",
    "import { readFileSync } from 'node:fs';",
    "import path from 'path';",
    "import { revalidatePath } from 'next/cache';",
    "import { secret } from '@/lib/secret.server';",
    "import logo from './logo.png';",
    "export const metadata = { title: 'x' };",
    "export const dynamic = 'force-dynamic';",
    'const SHOTS = { a: { src: logo.src } };',
    "const LOCAL = '/brand.svg';",
    'export default function P({ k }) {',
    '  void cookies; void readFileSync; void path; void revalidatePath;',
    '  const env = process.env;',
    '  return (',
    '    <div data-s={secret + process.env.API_SECRET + process.env[k] + String(env)}>',
    '      <img src="/logo.png" alt="" />',
    '      <img src={SHOTS.a.src} alt="" />',
    '      <img src={LOCAL} alt="" />',
    '    </div>',
    '  );',
    '}',
  ),
  'app/client/logo.png': '',
  'lib/secret.server.ts': 'export const secret = "s";',
  'app/conf/page.tsx': lines(
    "import { late } from '@/components/late';",
    "export const dynamic = 'sometimes';",
    "export const runtime = 'edge';",
    'export const revalidate = Number(process.env.TTL);',
    'export const experimental_ppr = true;',
    "export const prefetch = 'partial';",
    "export const preferredRegion = 'home';",
    'export const maxDuration = 0;',
    "export const dynamicParams = 'yes';",
    'export default function P() { return <p>{late()}</p>; }',
  ),
  'components/late.tsx': lines(
    "import { x } from './x';",
    "'use client';",
    "export function late() { 'use client'; return x; }",
  ),
  'components/x.ts': 'export const x = 1;',
  'app/legacy/page.tsx': lines(
    "import { useRouter } from 'next/router';",
    'export default function P() { useRouter(); return null; }',
  ),
};

test('negative tree: reports every rule', (t) => {
  const root = tree(t, NEGATIVE);
  const findings = check({ root });
  const byRule = (rule) => findings.filter((f) => f.rule === rule);
  assert.deepEqual([...new Set(findings.map((f) => f.rule))].sort(), [
    'next-client-api-in-server',
    'next-client-metadata',
    'next-directive-misplaced',
    'next-img-element',
    'next-pages-api-in-app',
    'next-segment-config-invalid',
    'next-segment-config-misplaced',
    'next-server-api-in-client',
    'next-server-env-in-client',
  ]);
  assert.deepEqual(
    byRule('next-client-api-in-server')
      .map((f) => f.message.split(' ')[0])
      .sort(),
    ['"client-only"', '<button', 'next/dynamic', 'the', 'usePathname', 'useState'],
  );
  assert.deepEqual(byRule('next-server-api-in-client').length, 5);
  assert.match(
    byRule('next-server-api-in-client').find((f) => f.message.includes('secret')).message,
    /the server module lib\/secret\.server\.ts is imported in the client graph \(reached from app\/client\/page\.tsx\)|the server module lib\/secret\.server\.ts is imported in the client graph$/,
  );
  assert.deepEqual(
    byRule('next-server-env-in-client')
      .map((f) => f.message.split(' ')[0])
      .sort(),
    ['process.env', 'process.env', 'process.env.API_SECRET'],
  );
  assert.deepEqual(
    byRule('next-segment-config-invalid')
      .map((f) => f.message.split('"')[1])
      .sort(),
    [
      'dynamic',
      'dynamicParams',
      'experimental_ppr',
      'maxDuration',
      'preferredRegion',
      'prefetch',
      'revalidate',
      'runtime',
    ],
  );
  assert.deepEqual(
    byRule('next-segment-config-misplaced')
      .map((f) => f.path)
      .sort(),
    ['app/client/page.tsx', 'app/helper.ts'],
  );
  assert.equal(byRule('next-client-metadata')[0].path, 'app/client/page.tsx');
  assert.equal(byRule('next-directive-misplaced').length, 2);
  assert.equal(byRule('next-img-element').length, 3);
  assert.deepEqual(
    byRule('next-pages-api-in-app')
      .map((f) => f.path)
      .sort(),
    ['app/layout.tsx', 'app/legacy/page.tsx'],
  );
});

test('cacheComponents: the previous caching options are removed, prefetch and instant apply', (t) => {
  const root = tree(t, {
    'next.config.ts': 'export default { cacheComponents: true };',
    'app/layout.tsx': lines(
      "export const prefetch = 'partial';",
      'export const instant = { level: "warning" };',
      'export default function L({ children }) { return <html><body>{children}</body></html>; }',
    ),
    'app/page.tsx': lines(
      "export const dynamic = 'force-static';",
      "export const prefetch = 'auto';",
      'export const instant = 3;',
      'export default function P() { return null; }',
    ),
  });
  assert.deepEqual(
    check({ root }).map((f) => `${f.path} ${f.message}`),
    [
      'app/page.tsx segment config "dynamic" is removed when cacheComponents is enabled',
      `app/page.tsx segment config "prefetch" takes 'partial' | 'force-disabled', not "auto"`,
      'app/page.tsx segment config "instant" takes true, false or an options object',
    ],
  );
});

test('a shared module is checked on both sides; a module only the client reaches may use hooks', (t) => {
  const root = tree(t, {
    'app/layout.tsx': lines(
      "import { Shared } from '../components/shared';",
      'export default function L({ children }) { return <html><body><Shared />{children}</body></html>; }',
    ),
    'app/page.tsx': lines(
      "import { Box } from '../components/box';",
      'export default function P() { return <Box />; }',
    ),
    'components/box.tsx': lines(
      "'use client';",
      "import { Shared } from './shared';",
      'export const Box = () => <Shared />;',
    ),
    'components/shared.tsx': lines(
      "import { useId } from 'react';",
      "import { useRef } from 'react';",
      'export function Shared() { useRef(null); return <i id={useId()} />; }',
    ),
  });
  const findings = check({ root });
  assert.deepEqual(
    findings.map((f) => `${f.path}:${String(f.line)} ${f.rule}`),
    ['components/shared.tsx:2 next-client-api-in-server'],
  );
});

test('no app directory: skips; a named --app or --next-config that does not exist is a configuration error', (t) => {
  const root = tree(t, { 'src/index.ts': '', 'app2/page.tsx': '' });
  assert.deepEqual(check({ root }), []);
  assert.throws(() => check({ root, options: { app: 'web/app' } }), /--app does not exist/);
  assert.throws(
    () => check({ root, options: { app: 'app2', 'next-config': 'nope.ts' } }),
    /--next-config/,
  );
  const run = spawnSync(process.execPath, [SCRIPT, `--root=${root}`], { encoding: 'utf8' });
  assert.equal(run.status, 0);
  assert.match(run.stderr, /^skipped: no targets/);
});

test('--server-modules names the server-only files by glob', (t) => {
  const root = tree(t, {
    'app/page.tsx': lines(
      "'use client';",
      "import { db } from '../lib/db';",
      'export default function P() { return db; }',
    ),
    'lib/db.ts': 'export const db = null;',
  });
  assert.deepEqual(check({ root }), []);
  const findings = check({ root, options: { 'server-modules': 'db.*,*.server.*' } });
  assert.deepEqual(
    findings.map((f) => f.rule),
    ['next-server-api-in-client'],
  );
});
