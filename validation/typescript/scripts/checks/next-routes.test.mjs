import { test } from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import { check } from './next-routes.mjs';

const SCRIPT = path.join(path.dirname(fileURLToPath(import.meta.url)), 'next-routes.mjs');

function tree(t, files) {
  const root = mkdtempSync(path.join(tmpdir(), 'next-routes-'));
  t.after(() => rmSync(root, { recursive: true, force: true }));
  for (const [file, text] of Object.entries(files)) {
    mkdirSync(path.dirname(path.join(root, file)), { recursive: true });
    writeFileSync(path.join(root, file), text);
  }
  return root;
}

const TSCONFIG = JSON.stringify({
  compilerOptions: { paths: { '@/*': ['./*'] }, jsx: 'preserve' },
});
const page = (body = '<p />') => `export default function Page() { return ${body}; }`;

/** A tree that follows every convention and whose navigation targets all resolve. */
const POSITIVE = {
  'tsconfig.json': TSCONFIG,
  'app/layout.tsx': [
    "import { Menu } from './_parts/menu';",
    'export default function RootLayout({ children }) {',
    '  return <html lang="en"><body><Menu />{children}</body></html>;',
    '}',
  ].join('\n'),
  'app/_parts/menu.tsx': [
    "'use client';",
    "import { useRouter } from 'next/navigation';",
    "import { NavLink } from '@/components/nav-link';",
    'export function Menu() {',
    '  const router = useRouter();',
    "  return <button onClick={() => router.push('/blog')}><NavLink /></button>;",
    '}',
  ].join('\n'),
  'components/nav-link.tsx': [
    "import Link from 'next/link';",
    'const slug = String(Date.now());',
    'export function NavLink() {',
    '  return (',
    '    <>',
    '      <Link href="/blog/first?ref=nav#top">first</Link>',
    '      <Link href={`/blog/${slug}`}>dynamic</Link>',
    '      <Link href="/shop">shop</Link>',
    '      <a href="https://example.com">external</a>',
    '      <a href="/report.pdf" download>file</a>',
    '      <a href="/api/items">api</a>',
    '      <a href="#top">top</a>',
    '    </>',
    '  );',
    '}',
  ].join('\n'),
  'app/page.tsx': page(),
  'app/(marketing)/about/page.tsx': [
    "import { redirect } from 'next/navigation';",
    "export default function About() { redirect('/blog'); }",
  ].join('\n'),
  'app/blog/page.tsx': page(),
  'app/blog/[slug]/page.tsx': [
    "import { notFound } from 'next/navigation';",
    'export default async function Post({ params }) {',
    '  const { slug } = await params;',
    "  if (slug === '') notFound();",
    '  return <p>{slug}</p>;',
    '}',
  ].join('\n'),
  'app/shop/[[...slug]]/page.tsx': page(),
  'app/shop/error.tsx':
    "'use client';\nexport default function E({ retry }) { return <button onClick={retry}>retry</button>; }",
  'app/photo/[id]/page.tsx': [
    'export const dynamicParams = false;',
    "export function generateStaticParams() { return [{ id: '1' }]; }",
    'export default function Photo() { return <p />; }',
  ].join('\n'),
  'app/@modal/default.tsx': 'export default function D() { return null; }',
  'app/@modal/(.)photo/[id]/page.tsx': page(),
  'app/@modal/(.)photo/[id]/not-found.tsx': page(),
  'app/api/items/route.ts': [
    "export const dynamic = 'force-dynamic';",
    'export async function GET() { return Response.json([]); }',
    'export const POST = async () => new Response(null, { status: 201 });',
    'export type Item = { id: string };',
  ].join('\n'),
  'app/icon.svg': '<svg/>',
  'public/report.pdf': '',
  'lib/routes.ts': [
    'export const PREFIX = { site: "", admin: "/admin" } as const;',
    'export const ROUTES = {',
    '  home: { template: "", trees: ["site"] },',
    '  post: { template: "/blog/{slug}", trees: ["site"] },',
    '  shop: { template: "/shop/:rest", trees: ["site"] },',
    '} satisfies Record<string, unknown>;',
    'export const LEGACY = { shop: "/shop/:rest", photo: "/photo/[id]", post: "/blog/$slug", about: "/about/" };',
  ].join('\n'),
  'nav-spec.json': JSON.stringify({
    items: [
      { label: 'Blog', path: '/blog', key: 'post' },
      { label: 'Group', key: 'home', children: [{ label: 'Shop', path: '/shop/a', key: 'shop' }] },
    ],
  }),
};

const POSITIVE_OPTIONS = {
  'route-table': 'lib/routes.ts#ROUTES',
  'route-table-prefixes': 'lib/routes.ts#PREFIX',
  'route-table-prefix-keys': 'trees',
  'nav-spec': 'nav-spec.json',
  'nav-route-key': 'true',
};

test('positive tree: every convention holds and every navigation target resolves', (t) => {
  const root = tree(t, { ...POSITIVE, 'app/admin/page.tsx': page() });
  assert.deepEqual(check({ root, options: POSITIVE_OPTIONS }), []);
});

/** A tree that breaks every rule once. */
const NEGATIVE = {
  'tsconfig.json': TSCONFIG,
  'app/(one)/layout.tsx':
    'export default function L({ children }) { return <main>{children}</main>; }',
  'app/(one)/dup/page.tsx': page(),
  'app/(two)/dup/page.tsx': page(),
  'app/a-[id]/page.tsx': page(),
  'app/docs/[...slug]/edit/page.tsx': page(),
  'app/twice/[id]/again/[id]/page.tsx': page(),
  'app/users/[id]/page.tsx': page(),
  'app/users/[name]/settings/page.tsx': page(),
  'app/mixed/page.tsx': page(),
  'app/mixed/route.ts': 'export function GET() { return new Response(); }',
  'app/sub/global-error.tsx': page(),
  'app/nodefault/page.tsx': 'export function Page() { return null; }',
  'app/err/error.tsx': page(),
  'app/err/page.tsx': page(),
  'app/api/x/route.ts': [
    'export default function handler() { return new Response(); }',
    'export const helper = 1;',
    "export * from './more';",
  ].join('\n'),
  'app/api/x/more.ts': 'export const GET = () => new Response();',
  'app/links/page.tsx': [
    "import Link from 'next/link';",
    "import { redirect, useRouter } from 'next/navigation';",
    'export default function Links({ id }) {',
    '  const router = useRouter();',
    "  router.replace('/nope');",
    "  useRouter().prefetch('/nope/either');",
    '  if (id) redirect(`/users/${id}/zzz`);',
    '  return <><Link href="/missing">m</Link><a href="/mixed">raw</a></>;',
    '}',
  ].join('\n'),
  'lib/routes.ts': 'export const ROUTES = { gone: { template: "/gone/{id}" }, links: "/links" };',
  'nav-spec.json': JSON.stringify({
    items: [
      { label: 'Gone', path: '/gone' },
      { label: 'Ghost', key: 'ghost' },
    ],
  }),
};

test('negative tree: reports every rule', (t) => {
  const root = tree(t, NEGATIVE);
  const findings = check({
    root,
    options: {
      'route-table': 'lib/routes.ts#ROUTES',
      'nav-spec': 'nav-spec.json',
      'nav-route-key': 'true',
    },
  });
  assert.deepEqual([...new Set(findings.map((f) => f.rule))].sort(), [
    'next-anchor-internal',
    'next-link-unresolved',
    'next-nav-unresolved',
    'next-nav-unrouted',
    'next-route-catch-all-not-last',
    'next-route-default-export',
    'next-route-document-tags',
    'next-route-duplicate',
    'next-route-dynamic-fallback',
    'next-route-error-not-client',
    'next-route-global-misplaced',
    'next-route-handler-export',
    'next-route-page-and-handler',
    'next-route-param-duplicate',
    'next-route-root-layout-missing',
    'next-route-segment-invalid',
    'next-route-slug-conflict',
    'next-route-table-unresolved',
  ]);
  const of = (rule) => findings.filter((f) => f.rule === rule);
  assert.deepEqual(
    of('next-route-segment-invalid').map((f) => f.path),
    ['app/a-[id]'],
  );
  assert.deepEqual(
    of('next-link-unresolved')
      .map((f) => f.message.split('"')[1])
      .sort(),
    ['/missing', '/nope', '/nope/either', '/users/${…}/zzz'],
  );
  assert.deepEqual(
    of('next-anchor-internal').map((f) => `${f.path}:${String(f.line)}`),
    ['app/links/page.tsx:8'],
  );
  assert.equal(of('next-route-duplicate')[0].path, 'app/(two)/dup/page.tsx');
  assert.equal(of('next-route-page-and-handler')[0].path, 'app/mixed/route.ts');
  assert.equal(of('next-route-handler-export').length, 3);
  assert.match(of('next-route-slug-conflict')[0].message, /"name" \(dynamic\) and "id" \(dynamic/);
  assert.deepEqual(
    of('next-route-dynamic-fallback')
      .map((f) => f.path)
      .sort(),
    [
      'app/docs/[...slug]/edit/page.tsx',
      'app/twice/[id]/again/[id]/page.tsx',
      'app/users/[id]/page.tsx',
      'app/users/[name]/settings/page.tsx',
    ],
  );
  assert.deepEqual(
    of('next-route-document-tags')
      .map((f) => f.path)
      .sort(),
    ['app/(one)/layout.tsx', 'app/sub/global-error.tsx'],
  );
  assert.deepEqual(
    of('next-route-error-not-client')
      .map((f) => f.path)
      .sort(),
    ['app/err/error.tsx', 'app/sub/global-error.tsx'],
  );
  assert.equal(of('next-route-table-unresolved')[0].path, 'lib/routes.ts');
  assert.match(of('next-route-table-unresolved')[0].message, /route "gone" \("\/gone\/\{\}"\)/);
  assert.deepEqual(
    of('next-nav-unrouted').map((f) => f.message),
    ['nav entry "Ghost" key "ghost" names no entry of the route table'],
  );
});

test('route table entries may be plain template strings with :name, [name] or $name parameters', (t) => {
  const root = tree(t, POSITIVE);
  assert.deepEqual(check({ root, options: { 'route-table': 'lib/routes.ts#LEGACY' } }), []);
});

test('a route table prefix with no page is reported per prefix', (t) => {
  const root = tree(t, POSITIVE);
  const findings = check({ root, options: { ...POSITIVE_OPTIONS, 'route-table-prefix-keys': '' } });
  assert.deepEqual(
    findings.map((f) => f.message),
    [
      'route "home" ("/admin" in "admin") resolves to no page',
      'route "post" ("/admin/blog/{}" in "admin") resolves to no page',
      'route "shop" ("/admin/shop/{}" in "admin") resolves to no page',
    ],
  );
});

test('no app directory: skips; a named --app that does not exist is a configuration error', (t) => {
  const root = tree(t, { 'src/index.ts': '' });
  assert.deepEqual(check({ root }), []);
  assert.throws(() => check({ root, options: { app: 'web/app' } }), /--app does not exist/);
  const run = spawnSync(process.execPath, [SCRIPT, `--root=${root}`], { encoding: 'utf8' });
  assert.equal(run.status, 0);
  assert.match(run.stderr, /^skipped: no targets/);
});

test('bad inputs: a route table that is not an object and --nav-route-key without a table exit 2', (t) => {
  const root = tree(t, {
    'app/layout.tsx': page('<html><body /></html>'),
    'lib/r.ts': 'export const R = [];',
  });
  assert.throws(
    () => check({ root, options: { 'route-table': 'lib/r.ts#R' } }),
    /not an object literal/,
  );
  assert.throws(
    () => check({ root, options: { 'route-table': 'lib/r.ts#Missing' } }),
    /no top-level const/,
  );
  assert.throws(() => check({ root, options: { 'nav-route-key': 'true' } }), /needs --route-table/);
  const run = spawnSync(process.execPath, [SCRIPT, `--root=${root}`, '--route-table=lib/r.ts'], {
    encoding: 'utf8',
  });
  assert.equal(run.status, 2);
  assert.match(run.stderr, /<file>#<ConstName>/);
});

test('CLI prints the finding contract and exits 1 on findings', (t) => {
  const root = tree(t, { 'app/page.tsx': page() });
  const run = spawnSync(process.execPath, [SCRIPT, `--root=${root}`], { encoding: 'utf8' });
  assert.equal(run.status, 1);
  assert.match(run.stdout, /^app\/page\.tsx:1:1: next-route-root-layout-missing: /m);
});
