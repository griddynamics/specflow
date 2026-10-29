import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';

import {
  appEntries,
  buildModuleGraph,
  compilerOptions,
  internalPathParts,
  matchesPattern,
  parseSegment,
  patternKey,
  scanAppTree,
  servedRoutes,
  urlPattern,
} from './next-app.mjs';

function tree(t, files) {
  const root = mkdtempSync(path.join(tmpdir(), 'next-app-'));
  t.after(() => rmSync(root, { recursive: true, force: true }));
  for (const [file, text] of Object.entries(files)) {
    mkdirSync(path.dirname(path.join(root, file)), { recursive: true });
    writeFileSync(path.join(root, file), text);
  }
  return root;
}

test('parseSegment classifies every folder convention and rejects malformed names', () => {
  const kinds = Object.fromEntries(
    [
      'blog',
      '(shop)',
      '[id]',
      '[...slug]',
      '[[...slug]]',
      '@modal',
      '_lib',
      '(.)photo',
      '(..)(..)photo',
      '(...)photo',
      'a-[id]',
      '[[id]]',
      '[]',
      '[...]',
      '(shop',
      '@',
      '[.x]',
    ].map((name) => [name, parseSegment(name).kind]),
  );
  assert.deepEqual(kinds, {
    blog: 'static',
    '(shop)': 'group',
    '[id]': 'dynamic',
    '[...slug]': 'catch-all',
    '[[...slug]]': 'optional-catch-all',
    '@modal': 'slot',
    _lib: 'private',
    '(.)photo': 'intercept',
    '(..)(..)photo': 'intercept',
    '(...)photo': 'intercept',
    'a-[id]': 'invalid',
    '[[id]]': 'invalid',
    '[]': 'invalid',
    '[...]': 'invalid',
    '(shop': 'invalid',
    '@': 'invalid',
    '[.x]': 'invalid',
  });
  assert.equal(parseSegment('[id]').param, 'id');
  assert.equal(parseSegment('(.)[id]').inner.kind, 'dynamic');
});

test('URL patterns drop groups and slots, and two group paths to one URL share a key', () => {
  const a = urlPattern(['(shop)', 'cart', '[id]'].map(parseSegment));
  const b = urlPattern(['(admin)', '@side', 'cart', '[item]'].map(parseSegment));
  assert.equal(patternKey(a.parts), '/cart/[]');
  assert.equal(patternKey(b.parts), patternKey(a.parts));
  assert.equal(b.slot, true);
  assert.equal(urlPattern(['(.)photo'].map(parseSegment)).intercepted, true);
});

test('internal paths: query, hash and trailing slash take no part; external and relative targets are not paths', () => {
  assert.deepEqual(internalPathParts('/a/b?x=1#h'), ['a', 'b']);
  assert.deepEqual(internalPathParts('/a/'), ['a']);
  assert.deepEqual(internalPathParts('/'), []);
  assert.deepEqual(internalPathParts('/?q=1'), []);
  for (const value of [
    '//cdn.example.com/x',
    'https://x.test/',
    'mailto:a@b.c',
    '#top',
    'a/b',
    '{}/x',
  ])
    assert.equal(internalPathParts(value), null, value);
});

test('matchesPattern: dynamic, catch-all and optional catch-all segments; {} interpolations are wildcards', () => {
  const p = (...names) => urlPattern(names.map(parseSegment)).parts;
  assert.ok(matchesPattern(p('blog', '[slug]'), ['blog', 'x']));
  assert.ok(!matchesPattern(p('blog', '[slug]'), ['blog']));
  assert.ok(!matchesPattern(p('blog', '[slug]'), ['blog', 'x', 'y']));
  assert.ok(matchesPattern(p('docs', '[...slug]'), ['docs', 'a', 'b']));
  assert.ok(!matchesPattern(p('docs', '[...slug]'), ['docs']));
  assert.ok(matchesPattern(p('shop', '[[...slug]]'), ['shop']));
  assert.ok(matchesPattern(p('shop', '[[...slug]]'), ['shop', 'a', 'b']));
  assert.ok(matchesPattern(p('blog', 'new'), ['blog', '{}']));
  assert.ok(matchesPattern(p('blog', 'new'), ['blog', 'n{}']));
  assert.ok(!matchesPattern(p('blog', 'new'), ['blog', 'x{}']));
});

test('servedRoutes: pages, route handlers, metadata files and public files; intercepting pages serve no URL', (t) => {
  const root = tree(t, {
    'app/page.tsx': '',
    'app/(m)/about/page.tsx': '',
    'app/api/x/route.ts': '',
    'app/icon.svg': '',
    'app/sitemap.ts': '',
    'app/@modal/(.)photo/page.tsx': '',
    'app/_private/page.tsx': '',
    'app/page.module.css': '',
    'public/files/a.pdf': '',
  });
  const scanned = scanAppTree(path.join(root, 'app'));
  const routes = servedRoutes(scanned, path.join(root, 'public'))
    .map((r) => `${r.kind} ${patternKey(r.parts)}`)
    .sort();
  assert.deepEqual(routes, [
    'metadata /icon.svg',
    'metadata /sitemap.xml',
    'page /',
    'page /about',
    'public /files/a.pdf',
    'route /api/x',
  ]);
});

test('module graph: a use client module and its imports are client, path aliases resolve, type-only imports are no edge', (t) => {
  const root = tree(t, {
    'tsconfig.json': JSON.stringify({
      compilerOptions: { paths: { '@/*': ['./*'] }, jsx: 'preserve' },
    }),
    'app/page.tsx': [
      "import { Button } from '@/components/button';",
      "import type { Row } from '@/lib/types-only';",
      "import { shared } from '../lib/shared';",
      'export default function Page() { return <Button label={shared} />; }',
    ].join('\n'),
    'components/button.tsx': [
      "'use client';",
      "import { shared } from '@/lib/shared';",
      'export const Button = ({ label }) => <button>{label}{shared}</button>;',
    ].join('\n'),
    'lib/shared.ts': "export const shared = 'x';",
    'lib/types-only.ts': 'export type Row = { id: string };',
    'proxy.ts': "import { shared } from './lib/shared'; export function proxy() { return shared; }",
  });
  const appDir = path.join(root, 'app');
  const graph = buildModuleGraph({
    root,
    entries: appEntries(root, appDir, scanAppTree(appDir)),
    options: compilerOptions(root),
  });
  const envs = Object.fromEntries(
    [...graph.values()].map((m) => [path.relative(root, m.file), [...m.envs].sort().join('+')]),
  );
  assert.deepEqual(envs, {
    'app/page.tsx': 'server',
    'components/button.tsx': 'client',
    'lib/shared.ts': 'client+server',
    'proxy.ts': 'server',
  });
  const button = [...graph.values()].find((m) => m.file.endsWith('button.tsx'));
  assert.equal(path.relative(root, button.via.client), 'app/page.tsx');
});

test('compilerOptions: a named tsconfig that does not exist is a configuration error', (t) => {
  const root = tree(t, { 'app/page.tsx': '' });
  assert.throws(() => compilerOptions(root, 'tsconfig.app.json'), /--tsconfig does not exist/);
  assert.equal(compilerOptions(root).allowJs, true);
});
