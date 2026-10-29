#!/usr/bin/env node
/**
 * `next-routes.mjs`: the Next.js App Router route tree follows the file
 * conventions, and every internal navigation target in the application
 * resolves to a URL the tree serves.
 *
 * Reads the app directory (`--app`, default `app`, else `src/app`) and the
 * modules reached from its entry files (TypeScript compiler API, imports
 * resolved through `tsconfig.json`). Conventions are those of the Next.js docs
 * in `next/dist/docs/01-app/03-api-reference/03-file-conventions`.
 *
 * Route tree rules:
 * - `next-route-segment-invalid`: a folder name is not a valid segment
 *   (`[id]`, `[...slug]`, `[[...slug]]`, `(group)`, `@slot`, `(.)x`, `_private`).
 * - `next-route-catch-all-not-last`: a catch-all segment has further URL
 *   segments below it.
 * - `next-route-param-duplicate`: a URL uses one parameter name twice.
 * - `next-route-slug-conflict`: two dynamic segments at the same URL position
 *   differ in name or kind.
 * - `next-route-duplicate`: two pages (or two route handlers) serve the same URL.
 * - `next-route-page-and-handler`: a page and a route handler serve the same URL.
 * - `next-route-root-layout-missing`: a page has no root layout above it.
 * - `next-route-document-tags`: a root layout, `global-error` or
 *   `global-not-found` does not render `<html>` and `<body>`.
 * - `next-route-global-misplaced`: `global-error` / `global-not-found` is not
 *   at the root of the app directory.
 * - `next-route-default-export`: a page, layout, template, loading, error,
 *   not-found, default, forbidden or unauthorized file has no default export.
 * - `next-route-handler-export`: a route handler has a default export, or
 *   exports a value that is not an HTTP method, segment config or
 *   `generateStaticParams`.
 * - `next-route-error-not-client`: an `error` / `global-error` file is not a
 *   Client Component.
 * - `next-route-dynamic-fallback`: a page under a dynamic segment has no
 *   strategy for a parameter that names nothing: no `error` / `not-found`
 *   boundary at or above its segment, no `notFound()` / `redirect()` /
 *   `permanentRedirect()` / `forbidden()` / `unauthorized()` call in the page
 *   or a layout above it, and no `generateStaticParams` with
 *   `dynamicParams = false`.
 *
 * Navigation rules (application modules only: those reached from the entries):
 * - `next-link-unresolved`: a static internal path given to `<Link href>`,
 *   `<a href>`, `redirect()` / `permanentRedirect()` or a `useRouter()`
 *   router's `push` / `replace` / `prefetch` resolves to no page, route
 *   handler, metadata route or `public/` file. Template literals match with
 *   their interpolations as wildcards.
 * - `next-anchor-internal`: a raw `<a href>` with a static path to a page
 *   (use `next/link`); a `download` link is not navigation.
 * - `next-route-table-unresolved`: an entry of the route table named by
 *   `--route-table` resolves to no page under one of its prefixes.
 * - `next-nav-unresolved`: a `--nav-spec` entry's `path` resolves to no page.
 * - `next-nav-unrouted`: with `--nav-route-key=true`, a `--nav-spec` entry's
 *   `key` names no route-table entry.
 *
 * usage: node next-routes.mjs [--root <dir>] [--app <dir>] [--public <dir>]
 *   [--page-extensions tsx,ts,jsx,js] [--tsconfig <file>]
 *   [--route-table <file>#<Name>] [--route-table-template <field>]
 *   [--route-table-prefixes <file>#<Name>] [--route-table-prefix-keys <field>]
 *   [--nav-spec <file>] [--nav-route-key true|false]
 */

import { existsSync, readFileSync } from 'node:fs';
import path from 'node:path';

import { finding } from './lib/report.mjs';
import { isMainModule, listOption, runScript, stringOption } from './lib/main.mjs';
import { relPath } from './lib/files.mjs';
import { pathLikeValue, staticStringValue, ts } from './lib/ts.mjs';
import { readJsonInput, requireArray } from './lib/spec-input.mjs';
import {
  COMPONENT_FILES,
  DEFAULT_PAGE_EXTENSIONS,
  HTTP_METHODS,
  SEGMENT_CONFIG,
  appEntries,
  buildModuleGraph,
  compilerOptions,
  findAppDir,
  importedLocals,
  internalPathParts,
  nodePosition,
  parseSegment,
  parseSource,
  patternKey,
  patternText,
  rendersDocument,
  resolvePath,
  scanAppTree,
  servedRoutes,
  urlPattern,
  valueExports,
} from './lib/next-app.mjs';

const DYNAMIC_KINDS = new Set(['dynamic', 'catch-all', 'optional-catch-all']);
const NAVIGATION_EXITS = ['notFound', 'redirect', 'permanentRedirect', 'forbidden', 'unauthorized'];
const ROUTER_METHODS = new Set(['push', 'replace', 'prefetch']);
/** Parameter placeholders a route-table template may use: `{id}`, `[id]`, `:id`, `$id`. */
const TEMPLATE_PARAM = /\{[^{}/]+\}|\[[^[\]/]+\]|:[A-Za-z_]\w*|\$[A-Za-z_]\w*/g;
const ALLOWED_HANDLER_EXPORTS = new Set([
  ...HTTP_METHODS,
  ...SEGMENT_CONFIG,
  'generateStaticParams',
]);

function inputsOf(root, options) {
  const appOption = stringOption(options, 'app', null);
  return {
    appOption,
    appDir: findAppDir(root, appOption),
    extensions: listOption(options, 'page-extensions', DEFAULT_PAGE_EXTENSIONS),
    publicDir: path.resolve(root, stringOption(options, 'public', 'public')),
    tsconfig: stringOption(options, 'tsconfig', null),
    routeTable: stringOption(options, 'route-table', null),
    templateField: stringOption(options, 'route-table-template', 'template'),
    prefixes: stringOption(options, 'route-table-prefixes', null),
    prefixKeys: stringOption(options, 'route-table-prefix-keys', null),
    navSpec: stringOption(options, 'nav-spec', null),
    navRouteKey: stringOption(options, 'nav-route-key', 'false') === 'true',
  };
}

/** Builds a finding at a node of a file, or at the file's first line without one. */
function reporter(root) {
  return (file, sourceFile, node, rule, message, extra) => {
    const { line, col } = node && sourceFile ? nodePosition(sourceFile, node) : { line: 1, col: 1 };
    return finding(relPath(root, file), line, col, rule, message, extra);
  };
}

function segmentFindings(tree, at) {
  const out = [];
  for (const dir of tree.dirs) {
    const name = dir.names.at(-1);
    if (name === undefined) continue;
    const parsed = parseSegment(name);
    if (parsed.kind === 'invalid') {
      out.push(
        at(
          dir.abs,
          null,
          null,
          'next-route-segment-invalid',
          `folder "${name}" is not a valid route segment: ${parsed.reason}`,
        ),
      );
    }
  }
  return out;
}

/** Catch-all position, repeated parameters and slug conflicts of one handler's URL. */
function urlShapeFindings(file, parts, dynamicAt, at, root) {
  const out = [];
  const label = patternText(parts);
  parts.forEach((p, i) => {
    if ((p.kind === 'catch-all' || p.kind === 'optional-catch-all') && i < parts.length - 1) {
      out.push(
        at(
          file.abs,
          null,
          null,
          'next-route-catch-all-not-last',
          `${label}: the catch-all segment "${p.param}" must be the last segment of the URL`,
        ),
      );
    }
  });
  const params = parts.filter((p) => DYNAMIC_KINDS.has(p.kind)).map((p) => p.param);
  for (const param of new Set(params.filter((p, i) => params.indexOf(p) !== i))) {
    out.push(
      at(
        file.abs,
        null,
        null,
        'next-route-param-duplicate',
        `${label}: the parameter "${param}" is used twice in one URL`,
      ),
    );
  }
  parts.forEach((p, i) => {
    if (!DYNAMIC_KINDS.has(p.kind)) return;
    const key = patternKey(parts.slice(0, i));
    const seen = dynamicAt.get(key) ?? new Map();
    dynamicAt.set(key, seen);
    const descriptor = `${p.kind}:${p.param}`;
    if (seen.has(descriptor)) return;
    const other = [...seen.values()][0];
    seen.set(descriptor, { file, p });
    if (!other) return;
    out.push(
      at(
        file.abs,
        null,
        null,
        'next-route-slug-conflict',
        `${label}: "${p.param}" (${p.kind}) and "${other.p.param}" (${other.p.kind}, ${relPath(root, other.file.abs)}) are dynamic segments at the same URL position`,
        { fix: 'use one parameter name and kind for one URL position' },
      ),
    );
  });
  return out;
}

function handlerFindings(root, tree, at) {
  const out = [];
  const byKey = new Map();
  const dynamicAt = new Map();
  for (const file of tree.files) {
    if (file.special !== 'page' && file.special !== 'route') continue;
    const { parts, intercepted, slot } = urlPattern(file.segments);
    out.push(...urlShapeFindings(file, parts, dynamicAt, at, root));
    if (intercepted || slot) continue;
    const key = patternKey(parts);
    const first = byKey.get(key);
    if (!first) {
      byKey.set(key, file);
      continue;
    }
    const mixed = first.special !== file.special;
    const label = patternText(parts);
    out.push(
      at(
        file.abs,
        null,
        null,
        mixed ? 'next-route-page-and-handler' : 'next-route-duplicate',
        mixed
          ? `${label} is served by a page and by a route handler (${relPath(root, first.abs)})`
          : `${label} is also served by ${relPath(root, first.abs)}`,
        { fix: 'give each URL exactly one page or route handler' },
      ),
    );
  }
  return out;
}

/** Whether every folder in `names` is a route group (a top-level root layout may sit there). */
const onlyGroups = (names) => names.every((n) => parseSegment(n).kind === 'group');
const isAncestorDir = (dir, file) =>
  file.dir === dir || file.dir.startsWith(dir.endsWith(path.sep) ? dir : dir + path.sep);

function layoutFindings(appDir, tree, graph, at) {
  const out = [];
  const layouts = tree.files.filter((f) => f.special === 'layout');
  const rootLayouts = new Set();
  for (const page of tree.files.filter((f) => f.special === 'page')) {
    const top = layouts
      .filter((l) => onlyGroups(l.names) && isAncestorDir(l.dir, page))
      .sort((a, b) => a.names.length - b.names.length)[0];
    if (top) rootLayouts.add(top);
    else
      out.push(
        at(
          page.abs,
          null,
          null,
          'next-route-root-layout-missing',
          'no root layout is above this page (app/layout, or a layout in a top-level route group)',
          { fix: 'add a layout that renders <html> and <body>' },
        ),
      );
  }
  const documents = [
    ...rootLayouts,
    ...tree.files.filter((f) => f.special === 'global-error' || f.special === 'global-not-found'),
  ];
  for (const file of documents) {
    const mod = graph.get(file.abs);
    if (mod && !rendersDocument(mod.sourceFile)) {
      out.push(
        at(
          file.abs,
          null,
          null,
          'next-route-document-tags',
          `${path.basename(file.abs)} replaces the document, so it must render <html> and <body>`,
        ),
      );
    }
  }
  for (const file of tree.files) {
    if (
      (file.special === 'global-error' || file.special === 'global-not-found') &&
      file.names.length
    ) {
      out.push(
        at(
          file.abs,
          null,
          null,
          'next-route-global-misplaced',
          `${file.special} is only read at the root of the app directory (${relPath(path.dirname(appDir), appDir)}/)`,
        ),
      );
    }
  }
  return out;
}

function exportFindings(tree, graph, at) {
  const out = [];
  for (const file of tree.files) {
    if (!file.special) continue;
    const mod = graph.get(file.abs);
    if (!mod) continue;
    const exports = valueExports(mod.sourceFile);
    const hasDefault = exports.some((e) => e.name === 'default');
    if (COMPONENT_FILES.has(file.special) && !hasDefault) {
      out.push(
        at(
          file.abs,
          null,
          null,
          'next-route-default-export',
          `${file.special} files must default-export a React component`,
        ),
      );
    }
    if (
      (file.special === 'error' || file.special === 'global-error') &&
      mod.directive !== 'use client'
    ) {
      out.push(
        at(
          file.abs,
          null,
          null,
          'next-route-error-not-client',
          `${file.special} is an error boundary and must be a Client Component`,
          { fix: "put 'use client' at the top of the file" },
        ),
      );
    }
    if (file.special !== 'route') continue;
    for (const e of exports) {
      if (ALLOWED_HANDLER_EXPORTS.has(e.name)) continue;
      const what =
        e.name === 'default'
          ? 'a default export'
          : e.name === '*'
            ? 'every export of another module'
            : `"${e.name}"`;
      out.push(
        at(
          file.abs,
          mod.sourceFile,
          e.node,
          'next-route-handler-export',
          `a route handler exports ${what}; it may export only HTTP method handlers (${[...HTTP_METHODS].join(', ')}), segment config and generateStaticParams`,
          { fix: 'move other values to a module beside it' },
        ),
      );
    }
  }
  return out;
}

/** Whether a module calls one of the next/navigation exits (`notFound()`, `redirect()`, ...). */
function callsNavigationExit(mod) {
  const locals = importedLocals(mod, 'next/navigation', NAVIGATION_EXITS);
  if (locals.size === 0) return false;
  let found = false;
  const visit = (node) => {
    if (found) return;
    if (
      ts.isCallExpression(node) &&
      ts.isIdentifier(node.expression) &&
      locals.has(node.expression.text)
    )
      found = true;
    else ts.forEachChild(node, visit);
  };
  visit(mod.sourceFile);
  return found;
}

function exportedLiteral(mod, name) {
  const e = valueExports(mod.sourceFile).find((x) => x.name === name);
  if (!e) return undefined;
  if (!e.value) return null;
  if (e.value.kind === ts.SyntaxKind.FalseKeyword) return false;
  if (e.value.kind === ts.SyntaxKind.TrueKeyword) return true;
  return e.value;
}

function fallbackFindings(tree, graph, at) {
  const out = [];
  const boundaries = tree.files.filter(
    (f) => f.special === 'error' || f.special === 'not-found' || f.special === 'global-error',
  );
  const layouts = tree.files.filter((f) => f.special === 'layout');
  for (const page of tree.files.filter((f) => f.special === 'page')) {
    const dynamic = urlPattern(page.segments).parts.find((p) => DYNAMIC_KINDS.has(p.kind));
    if (!dynamic) continue;
    if (boundaries.some((b) => isAncestorDir(b.dir, page))) continue;
    const chain = [page, ...layouts.filter((l) => isAncestorDir(l.dir, page))]
      .map((f) => graph.get(f.abs))
      .filter(Boolean);
    if (chain.some(callsNavigationExit)) continue;
    const pageMod = graph.get(page.abs);
    const staticOnly =
      pageMod &&
      exportedLiteral(pageMod, 'generateStaticParams') !== undefined &&
      chain.some((m) => exportedLiteral(m, 'dynamicParams') === false);
    if (staticOnly) continue;
    out.push(
      at(
        page.abs,
        null,
        null,
        'next-route-dynamic-fallback',
        `${patternText(urlPattern(page.segments).parts)} takes the parameter "${dynamic.param}" but has no strategy for a value that names nothing`,
        {
          fix: 'add an error.tsx or not-found.tsx boundary at or above the segment, call notFound() or redirect() for an unknown value, or pre-render with generateStaticParams and dynamicParams = false',
        },
      ),
    );
  }
  return out;
}

/** A string or template literal's path value (`{}` for each interpolation), or `null`. */
function literalPath(node) {
  if (!node) return null;
  let expr = node;
  if (ts.isJsxExpression(expr)) expr = expr.expression;
  while (expr && (ts.isParenthesizedExpression(expr) || ts.isAsExpression(expr)))
    expr = expr.expression;
  if (!expr) return null;
  return pathLikeValue(expr);
}

function jsxAttribute(element, name) {
  for (const prop of element.attributes.properties) {
    if (ts.isJsxAttribute(prop) && ts.isIdentifier(prop.name) && prop.name.text === name)
      return prop;
  }
  return null;
}

/** Every static navigation target of one module: `{ node, value, anchor, download }`. */
function navigationTargets(mod) {
  const out = [];
  const linkLocals = new Set(
    mod.imports
      .filter((e) => e.specifier === 'next/link' && !e.typeOnly)
      .flatMap((e) => e.names.filter((n) => n.name === 'default' && n.local).map((n) => n.local)),
  );
  const exits = importedLocals(mod, 'next/navigation', ['redirect', 'permanentRedirect']);
  const routerHooks = importedLocals(mod, 'next/navigation', ['useRouter']);
  const routers = new Set();
  const isRouterCall = (node) =>
    ts.isCallExpression(node) &&
    ts.isIdentifier(node.expression) &&
    routerHooks.has(node.expression.text);
  const visit = (node) => {
    if (
      ts.isVariableDeclaration(node) &&
      ts.isIdentifier(node.name) &&
      node.initializer &&
      isRouterCall(node.initializer)
    )
      routers.add(node.name.text);
    if (ts.isJsxOpeningElement(node) || ts.isJsxSelfClosingElement(node)) {
      const tag = ts.isIdentifier(node.tagName) ? node.tagName.text : null;
      if (tag === 'a' || (tag && linkLocals.has(tag))) {
        const href = jsxAttribute(node, 'href');
        const value = href ? literalPath(href.initializer) : null;
        if (value !== null)
          out.push({
            node: href,
            value,
            anchor: tag === 'a',
            download: jsxAttribute(node, 'download') !== null,
          });
      }
    }
    ts.forEachChild(node, visit);
  };
  visit(mod.sourceFile);
  const calls = (node) => {
    if (ts.isCallExpression(node) && node.arguments.length > 0) {
      const callee = node.expression;
      const exit = ts.isIdentifier(callee) && exits.has(callee.text);
      const routerMethod =
        ts.isPropertyAccessExpression(callee) &&
        ROUTER_METHODS.has(callee.name.text) &&
        ((ts.isIdentifier(callee.expression) && routers.has(callee.expression.text)) ||
          isRouterCall(callee.expression));
      const value = exit || routerMethod ? literalPath(node.arguments[0]) : null;
      if (value !== null)
        out.push({ node: node.arguments[0], value, anchor: false, download: false });
    }
    ts.forEachChild(node, calls);
  };
  calls(mod.sourceFile);
  return out;
}

function linkFindings(graph, routes, at) {
  const out = [];
  for (const mod of graph.values()) {
    for (const target of navigationTargets(mod)) {
      const parts = internalPathParts(target.value);
      if (parts === null) continue;
      const shown = target.value.replaceAll('{}', '${…}');
      const matches = resolvePath(routes, parts);
      if (matches.length === 0) {
        out.push(
          at(
            mod.file,
            mod.sourceFile,
            target.node,
            'next-link-unresolved',
            `"${shown}" resolves to no page, route handler, metadata route or public file`,
          ),
        );
        continue;
      }
      if (target.anchor && !target.download && matches.some((m) => m.kind === 'page')) {
        out.push(
          at(
            mod.file,
            mod.sourceFile,
            target.node,
            'next-anchor-internal',
            `a raw <a> links to the page "${shown}": it reloads the whole document`,
            { fix: "render <Link> from 'next/link' for an in-app page" },
          ),
        );
      }
    }
  }
  return out;
}

/** Reads `file#Name`: the object literal a top-level `const Name = {...}` holds. */
function namedObject(root, ref, label) {
  const [file, name] = ref.split('#');
  if (!file || !name) throw new TypeError(`${label} must be <file>#<ConstName>`);
  const abs = path.resolve(root, file);
  if (!existsSync(abs)) throw new Error(`${label} does not exist: ${file}`);
  const sourceFile = parseSource(abs, readFileSync(abs, 'utf8'));
  for (const statement of sourceFile.statements) {
    if (!ts.isVariableStatement(statement)) continue;
    for (const decl of statement.declarationList.declarations) {
      if (!ts.isIdentifier(decl.name) || decl.name.text !== name) continue;
      let init = decl.initializer;
      while (
        init &&
        (ts.isAsExpression(init) ||
          ts.isSatisfiesExpression(init) ||
          ts.isParenthesizedExpression(init))
      )
        init = init.expression;
      if (init && ts.isObjectLiteralExpression(init)) return { abs, sourceFile, object: init };
      throw new TypeError(`${label}: ${name} in ${file} is not an object literal`);
    }
  }
  throw new TypeError(`${label}: no top-level const ${name} in ${file}`);
}

function propertyName(prop) {
  if (!prop.name) return null;
  if (ts.isIdentifier(prop.name) || ts.isStringLiteral(prop.name)) return prop.name.text;
  return null;
}

function objectProperty(object, name) {
  for (const prop of object.properties) {
    if (ts.isPropertyAssignment(prop) && propertyName(prop) === name) return prop.initializer;
  }
  return null;
}

/** The route table entries: `{ key, node, template, prefixKeys }`. */
function routeTable(root, inputs) {
  const { sourceFile, object, abs } = namedObject(root, inputs.routeTable, '--route-table');
  const entries = [];
  for (const prop of object.properties) {
    if (!ts.isPropertyAssignment(prop)) continue;
    const key = propertyName(prop);
    let template = staticStringValue(prop.initializer);
    let prefixKeys = null;
    if (template === null && ts.isObjectLiteralExpression(prop.initializer)) {
      const t = objectProperty(prop.initializer, inputs.templateField);
      template = t ? staticStringValue(t) : null;
      const keys = inputs.prefixKeys ? objectProperty(prop.initializer, inputs.prefixKeys) : null;
      if (keys && ts.isArrayLiteralExpression(keys))
        prefixKeys = keys.elements.map(staticStringValue).filter((v) => v !== null);
    }
    if (key !== null && template !== null) entries.push({ key, node: prop, template, prefixKeys });
  }
  let prefixes = new Map([['', '']]);
  if (inputs.prefixes) {
    const map = namedObject(root, inputs.prefixes, '--route-table-prefixes');
    prefixes = new Map();
    for (const prop of map.object.properties) {
      if (!ts.isPropertyAssignment(prop)) continue;
      const value = staticStringValue(prop.initializer);
      const key = propertyName(prop);
      if (key !== null && value !== null) prefixes.set(key, value);
    }
  }
  return { abs, sourceFile, entries, prefixes };
}

function routeTableFindings(table, routes, at) {
  const out = [];
  for (const entry of table.entries) {
    const keys = entry.prefixKeys ?? [...table.prefixes.keys()];
    for (const prefixKey of keys) {
      const prefix = table.prefixes.get(prefixKey);
      if (prefix === undefined) continue;
      const url = `${prefix}${entry.template.replace(TEMPLATE_PARAM, '{}')}` || '/';
      const parts = internalPathParts(url.startsWith('/') ? url : `/${url}`);
      if (parts !== null && resolvePath(routes, parts, ['page']).length) continue;
      out.push(
        at(
          table.abs,
          table.sourceFile,
          entry.node,
          'next-route-table-unresolved',
          `route "${entry.key}" ("${url}"${prefixKey ? ` in "${prefixKey}"` : ''}) resolves to no page`,
        ),
      );
    }
  }
  return out;
}

function navItems(items, out = []) {
  for (const item of requireArray(items, '--nav-spec "items"')) {
    out.push(item);
    if (item.children) navItems(item.children, out);
  }
  return out;
}

function navFindings(root, inputs, table, routes) {
  const spec = readJsonInput(root, inputs.navSpec, '--nav-spec');
  const out = [];
  const keys = new Set(table ? table.entries.map((e) => e.key) : []);
  for (const item of navItems(spec.items)) {
    const label = typeof item.label === 'string' ? item.label : JSON.stringify(item);
    if (typeof item.path === 'string') {
      const parts = internalPathParts(item.path);
      if (parts === null || resolvePath(routes, parts, ['page']).length === 0) {
        out.push(
          finding(
            inputs.navSpec,
            1,
            1,
            'next-nav-unresolved',
            `nav entry "${label}" path "${item.path}" resolves to no page`,
          ),
        );
      }
    }
    if (inputs.navRouteKey && typeof item.key === 'string' && !keys.has(item.key)) {
      out.push(
        finding(
          inputs.navSpec,
          1,
          1,
          'next-nav-unrouted',
          `nav entry "${label}" key "${item.key}" names no entry of the route table`,
        ),
      );
    }
  }
  return out;
}

export function check({ root, options = {} }) {
  const inputs = inputsOf(root, options);
  if (!inputs.appDir) {
    if (inputs.appOption) throw new Error(`--app does not exist: ${inputs.appOption}`);
    return [];
  }
  if (inputs.navRouteKey && !inputs.routeTable)
    throw new TypeError('--nav-route-key=true needs --route-table');
  const at = reporter(root);
  const tree = scanAppTree(inputs.appDir, inputs.extensions);
  const graph = buildModuleGraph({
    root,
    entries: appEntries(root, inputs.appDir, tree),
    options: compilerOptions(root, inputs.tsconfig),
    readText: (file) => readFileSync(file, 'utf8'),
  });
  const routes = servedRoutes(tree, inputs.publicDir);
  const table = inputs.routeTable ? routeTable(root, inputs) : null;
  return [
    ...segmentFindings(tree, at),
    ...handlerFindings(root, tree, at),
    ...layoutFindings(inputs.appDir, tree, graph, at),
    ...exportFindings(tree, graph, at),
    ...fallbackFindings(tree, graph, at),
    ...linkFindings(graph, routes, at),
    ...(table ? routeTableFindings(table, routes, at) : []),
    ...(inputs.navSpec ? navFindings(root, inputs, table, routes) : []),
  ];
}

function skipIf({ root, options }) {
  const { appDir, appOption } = inputsOf(root, options);
  return appDir || appOption ? null : 'no targets: no app directory (app/ or src/app/)';
}

if (isMainModule(import.meta.url)) runScript(check, { skipIf });
