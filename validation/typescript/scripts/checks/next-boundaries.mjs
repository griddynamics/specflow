#!/usr/bin/env node
/**
 * `next-boundaries.mjs`: the Server and Client Component boundary of a
 * Next.js App Router application holds, and the framework APIs are used where
 * they work.
 *
 * Builds the module graph reached from the app directory's entry files
 * (`page`, `layout`, `route`, ... and `proxy` / `middleware` /
 * `instrumentation`) with the TypeScript compiler API and the project's
 * `tsconfig.json`, and gives every module its environment: server, or client
 * from a `'use client'` module down. A module reached through both is checked
 * as both. Type-only imports are erased and are not edges.
 *
 * Rules:
 * - `next-client-api-in-server`: a module in the server graph imports a
 *   client-only API (`useState`, `useEffect`, ... from `react`; `flushSync`,
 *   `useFormStatus`, ... from `react-dom`; `useRouter`, `usePathname`, ...
 *   from `next/navigation`; `client-only`; `react-dom/client`), calls
 *   `next/dynamic` with `ssr: false`, or (server graph only) passes an event
 *   handler to a DOM element or reads a browser global (`window`, `document`,
 *   `localStorage`, `sessionStorage`, `navigator`) outside a `typeof` guard.
 * - `next-server-api-in-client`: a module in the client graph imports
 *   `server-only`, `next/headers`, `next/server`, a server function of
 *   `next/cache`, a Node.js built-in, or a module whose file name matches
 *   `--server-modules` (default `*.server.*`).
 * - `next-server-env-in-client`: a module in the client graph reads
 *   `process.env.NAME` without the `NEXT_PUBLIC_` prefix (Next.js replaces it
 *   with an empty string), or reads `process.env` dynamically (never inlined).
 * - `next-client-metadata`: a `'use client'` route file exports `metadata`,
 *   `generateMetadata`, `viewport`, `generateViewport` or `generateStaticParams`
 *   (Server Component only).
 * - `next-segment-config-invalid`: a route segment config export of a page,
 *   layout or route handler is not a literal Next.js can read at build time,
 *   has a value the option does not take, is deprecated or removed, or does
 *   not apply under the project's `cacheComponents` setting (read from
 *   `next.config.*`).
 * - `next-segment-config-misplaced`: segment config is exported from a file
 *   Next.js does not read it from (any app-directory file but a page, layout
 *   or route handler, or a Client Component).
 * - `next-directive-misplaced`: a `'use client'` / `'use server'` directive
 *   that is not in the module's directive prologue, or a `'use client'` inside
 *   a function.
 * - `next-img-element`: a raw `<img>` whose source is a local asset the check
 *   can see (a static image import, a value built from one, or a literal
 *   local path); `next/image` sizes and optimises it. Inside `<picture>` and
 *   in image metadata routes (`opengraph-image`, `icon`, ...) a raw `<img>` is
 *   the convention.
 * - `next-pages-api-in-app`: an App Router module imports a Pages Router API
 *   (`next/router`, `next/head`, `next/document`).
 *
 * usage: node next-boundaries.mjs [--root <dir>] [--app <dir>]
 *   [--page-extensions tsx,ts,jsx,js] [--tsconfig <file>]
 *   [--server-modules <glob,...>] [--next-config <file>]
 */

import { existsSync, readFileSync } from 'node:fs';
import { builtinModules } from 'node:module';
import path from 'node:path';

import { finding } from './lib/report.mjs';
import { isMainModule, listOption, runScript, stringOption } from './lib/main.mjs';
import { relPath } from './lib/files.mjs';
import { staticStringValue, ts } from './lib/ts.mjs';
import {
  DEFAULT_PAGE_EXTENSIONS,
  SEGMENT_CONFIG,
  appEntries,
  buildModuleGraph,
  compilerOptions,
  findAppDir,
  importedLocals,
  nodePosition,
  scanAppTree,
  valueExports,
} from './lib/next-app.mjs';

/** React APIs the `react-server` build does not provide (Server Components cannot use them). */
const CLIENT_ONLY_APIS = {
  react: [
    'Component',
    'PureComponent',
    'createContext',
    'createFactory',
    'useActionState',
    'useDeferredValue',
    'useEffect',
    'useEffectEvent',
    'useImperativeHandle',
    'useInsertionEffect',
    'useLayoutEffect',
    'useOptimistic',
    'useReducer',
    'useRef',
    'useState',
    'useSyncExternalStore',
    'useTransition',
    'experimental_useOptimistic',
  ],
  'react-dom': ['flushSync', 'unstable_batchedUpdates', 'useFormState', 'useFormStatus'],
  'next/navigation': [
    'useLinkStatus',
    'useParams',
    'usePathname',
    'useRouter',
    'useSearchParams',
    'useSelectedLayoutSegment',
    'useSelectedLayoutSegments',
    'useServerInsertedHTML',
  ],
};
const CLIENT_ONLY_MODULES = new Set(['client-only', 'react-dom/client']);
const SERVER_ONLY_MODULES = new Set(['server-only', 'next/headers', 'next/server']);
const SERVER_CACHE_APIS = new Set([
  'unstable_cache',
  'revalidatePath',
  'revalidateTag',
  'updateTag',
  'refresh',
  'cacheLife',
  'cacheTag',
  'unstable_cacheLife',
  'unstable_cacheTag',
]);
const PAGES_ROUTER_MODULES = new Set(['next/router', 'next/head', 'next/document']);
const BROWSER_GLOBALS = new Set([
  'window',
  'document',
  'localStorage',
  'sessionStorage',
  'navigator',
]);
const NODE_BUILTINS = new Set(builtinModules);
/** Process variables Next.js inlines into the client bundle besides `NEXT_PUBLIC_*`. */
const INLINED_ENV = new Set(['NODE_ENV']);
const SERVER_ONLY_EXPORTS = [
  'metadata',
  'generateMetadata',
  'viewport',
  'generateViewport',
  'generateStaticParams',
];
const CONFIG_FILES = ['page', 'layout', 'route'];
const IMAGE_FILE = /\.(?:apng|avif|bmp|gif|ico|jpe?g|png|svg|webp)$/i;
const IMAGE_ROUTES = new Set(['opengraph-image', 'twitter-image', 'icon', 'apple-icon']);

function inputsOf(root, options) {
  const appOption = stringOption(options, 'app', null);
  return {
    appOption,
    appDir: findAppDir(root, appOption),
    extensions: listOption(options, 'page-extensions', DEFAULT_PAGE_EXTENSIONS),
    tsconfig: stringOption(options, 'tsconfig', null),
    serverModules: listOption(options, 'server-modules', ['*.server.*']),
    nextConfig: stringOption(options, 'next-config', null),
  };
}

/** A file-name glob (`*`, `?`) as a regular expression on the base name. */
function globRe(glob) {
  const body = glob
    .split('')
    .map((ch) =>
      ch === '*' ? '[^/]*' : ch === '?' ? '[^/]' : ch.replace(/[.+^${}()|[\]\\]/g, '\\$&'),
    )
    .join('');
  return new RegExp(`^${body}$`);
}

/** Whether the project's next.config turns `cacheComponents` on. */
function cacheComponentsOn(root, option) {
  const candidates = option
    ? [option]
    : ['next.config.ts', 'next.config.mjs', 'next.config.js', 'next.config.cjs', 'next.config.mts'];
  for (const candidate of candidates) {
    const abs = path.resolve(root, candidate);
    if (existsSync(abs)) return /\bcacheComponents\s*:\s*true\b/.test(readFileSync(abs, 'utf8'));
  }
  if (option) throw new Error(`--next-config does not exist: ${option}`);
  return false;
}

function reporter(root) {
  return (mod, node, rule, message, extra) => {
    const { line, col } = node ? nodePosition(mod.sourceFile, node) : { line: 1, col: 1 };
    return finding(relPath(root, mod.file), line, col, rule, message, extra);
  };
}

const viaText = (root, mod, env) =>
  mod.via[env] ? ` (reached from ${relPath(root, mod.via[env])})` : '';

function serverFindings(root, mod, at) {
  const out = [];
  const via = viaText(root, mod, 'server');
  for (const edge of mod.imports) {
    if (edge.typeOnly) continue;
    if (CLIENT_ONLY_MODULES.has(edge.specifier)) {
      out.push(
        at(
          mod,
          edge.node,
          'next-client-api-in-server',
          `"${edge.specifier}" is imported in the server graph${via}`,
          {
            fix: "move the code into a 'use client' module",
          },
        ),
      );
      continue;
    }
    const names = CLIENT_ONLY_APIS[edge.specifier];
    if (!names) continue;
    for (const n of edge.names) {
      if (!names.includes(n.name)) continue;
      out.push(
        at(
          mod,
          n.node,
          'next-client-api-in-server',
          `${n.name} from "${edge.specifier}" only works in a Client Component, and this module is in the server graph${via}`,
          { fix: "add 'use client' to the component that needs it, or move the call into one" },
        ),
      );
    }
  }
  const dynamicLocals = new Set(
    mod.imports
      .filter((e) => e.specifier === 'next/dynamic' && !e.typeOnly)
      .flatMap((e) => e.names.filter((n) => n.name === 'default' && n.local).map((n) => n.local)),
  );
  const serverOnly = mod.envs.size === 1;
  const visit = (node) => {
    if (
      ts.isCallExpression(node) &&
      ts.isIdentifier(node.expression) &&
      dynamicLocals.has(node.expression.text) &&
      node.arguments[1] &&
      ts.isObjectLiteralExpression(node.arguments[1]) &&
      node.arguments[1].properties.some(
        (p) =>
          ts.isPropertyAssignment(p) &&
          ts.isIdentifier(p.name) &&
          p.name.text === 'ssr' &&
          p.initializer.kind === ts.SyntaxKind.FalseKeyword,
      )
    ) {
      out.push(
        at(
          mod,
          node,
          'next-client-api-in-server',
          `next/dynamic with ssr: false is not allowed in a Server Component${via}`,
          {
            fix: "call it from a 'use client' module",
          },
        ),
      );
    }
    if (
      serverOnly &&
      ts.isJsxAttribute(node) &&
      ts.isIdentifier(node.name) &&
      /^on[A-Z]/.test(node.name.text)
    ) {
      const element = node.parent.parent;
      const tag = ts.isIdentifier(element.tagName) ? element.tagName.text : '';
      const literal = node.initializer && ts.isStringLiteral(node.initializer);
      if (/^[a-z]/.test(tag) && node.initializer && !literal) {
        out.push(
          at(
            mod,
            node,
            'next-client-api-in-server',
            `<${tag} ${node.name.text}> is an event handler in a Server Component${via}`,
            {
              fix: "render the interactive element from a 'use client' component",
            },
          ),
        );
      }
    }
    if (
      serverOnly &&
      ts.isIdentifier(node) &&
      BROWSER_GLOBALS.has(node.text) &&
      isGlobalRead(node)
    ) {
      out.push(
        at(
          mod,
          node,
          'next-client-api-in-server',
          `the browser global "${node.text}" does not exist on the server${via}`,
          {
            fix: "read it in a 'use client' component (in an effect or an event handler)",
          },
        ),
      );
    }
    ts.forEachChild(node, visit);
  };
  visit(mod.sourceFile);
  return out;
}

/** Whether an identifier reads a global (not a property name, declaration, or `typeof` guard). */
function isGlobalRead(node) {
  const parent = node.parent;
  if (!parent) return false;
  if (ts.isPropertyAccessExpression(parent) && parent.name === node) return false;
  if (ts.isPropertyAssignment(parent) && parent.name === node) return false;
  if (ts.isShorthandPropertyAssignment(parent)) return false;
  if (ts.isTypeOfExpression(parent)) return false;
  if (
    (ts.isVariableDeclaration(parent) ||
      ts.isParameter(parent) ||
      ts.isFunctionDeclaration(parent) ||
      ts.isBindingElement(parent) ||
      ts.isImportSpecifier(parent) ||
      ts.isPropertySignature(parent) ||
      ts.isPropertyDeclaration(parent) ||
      ts.isMethodDeclaration(parent) ||
      ts.isJsxAttribute(parent)) &&
    parent.name === node
  )
    return false;
  if (ts.isTypeReferenceNode(parent) || ts.isTypeQueryNode(parent) || ts.isQualifiedName(parent))
    return false;
  return !isShadowed(node);
}

/** Whether a local binding of the same name is in scope (a parameter or a local declaration). */
function isShadowed(node) {
  const name = node.text;
  for (let scope = node.parent; scope; scope = scope.parent) {
    if (ts.isFunctionLike(scope)) {
      for (const p of scope.parameters ?? [])
        if (ts.isIdentifier(p.name) && p.name.text === name) return true;
    }
    if (ts.isBlock(scope) || ts.isSourceFile(scope)) {
      for (const statement of scope.statements) {
        if (ts.isVariableStatement(statement)) {
          for (const d of statement.declarationList.declarations)
            if (ts.isIdentifier(d.name) && d.name.text === name) return true;
        }
        if (
          (ts.isFunctionDeclaration(statement) || ts.isClassDeclaration(statement)) &&
          statement.name?.text === name
        )
          return true;
      }
    }
  }
  return false;
}

function clientFindings(root, mod, at, serverModuleRes) {
  const out = [];
  const via = viaText(root, mod, 'client');
  for (const edge of mod.imports) {
    if (edge.typeOnly) continue;
    const spec = edge.specifier;
    const bare = spec.startsWith('node:') ? spec.slice(5) : spec;
    let what = null;
    if (SERVER_ONLY_MODULES.has(spec)) what = `"${spec}"`;
    else if (spec === 'next/cache') {
      const used = edge.names.filter((n) => SERVER_CACHE_APIS.has(n.name) || n.name === '*');
      if (used.length) what = `${used.map((n) => n.name).join(', ')} from "next/cache"`;
    } else if (spec.startsWith('node:') || NODE_BUILTINS.has(bare))
      what = `the Node.js built-in "${spec}"`;
    else if (edge.resolved && serverModuleRes.some((re) => re.test(path.basename(edge.resolved))))
      what = `the server module ${relPath(root, edge.resolved)}`;
    if (what) {
      out.push(
        at(
          mod,
          edge.node,
          'next-server-api-in-client',
          `${what} is imported in the client graph${via}`,
          {
            fix: 'read it in a Server Component and pass the result down as props',
          },
        ),
      );
    }
  }
  const visit = (node) => {
    const env = processEnvAccess(node);
    if (env) {
      const ok =
        env.name !== null && (env.name.startsWith('NEXT_PUBLIC_') || INLINED_ENV.has(env.name));
      if (!ok) {
        const message =
          env.name === null
            ? `process.env is read dynamically in the client graph${via}: Next.js only inlines process.env.NEXT_PUBLIC_* read by name`
            : `process.env.${env.name} is read in the client graph${via}: Next.js replaces it with an empty string in the browser`;
        out.push(
          at(mod, node, 'next-server-env-in-client', message, {
            fix: 'read the value on the server and pass it down, or expose it as NEXT_PUBLIC_*',
          }),
        );
      }
      return;
    }
    ts.forEachChild(node, visit);
  };
  visit(mod.sourceFile);
  return out;
}

/** `process.env.NAME` -> `{ name }`; `process.env[x]` or a bare `process.env` -> `{ name: null }`. */
function processEnvAccess(node) {
  const isProcessEnv = (n) =>
    ts.isPropertyAccessExpression(n) &&
    ts.isIdentifier(n.expression) &&
    n.expression.text === 'process' &&
    n.name.text === 'env';
  if (ts.isPropertyAccessExpression(node) && isProcessEnv(node.expression))
    return { name: node.name.text };
  if (ts.isElementAccessExpression(node) && isProcessEnv(node.expression)) {
    const key = staticStringValue(node.argumentExpression);
    return { name: key };
  }
  if (isProcessEnv(node)) {
    const parent = node.parent;
    const accessed =
      (ts.isPropertyAccessExpression(parent) || ts.isElementAccessExpression(parent)) &&
      parent.expression === node;
    if (!accessed) return { name: null };
  }
  return null;
}

const STRING_VALUES = {
  dynamic: ['auto', 'force-dynamic', 'error', 'force-static'],
  fetchCache: [
    'auto',
    'default-cache',
    'only-cache',
    'force-cache',
    'force-no-store',
    'default-no-store',
    'only-no-store',
  ],
  runtime: ['nodejs', 'edge'],
  prefetch: ['partial', 'force-disabled'],
};
const LEGACY_CACHE_CONFIG = new Set(['dynamic', 'dynamicParams', 'revalidate', 'fetchCache']);
const CACHE_COMPONENTS_CONFIG = new Set(['prefetch', 'instant']);

function literalOf(node) {
  let expr = node;
  while (
    expr &&
    (ts.isAsExpression(expr) ||
      ts.isSatisfiesExpression(expr) ||
      ts.isParenthesizedExpression(expr))
  )
    expr = expr.expression;
  if (!expr) return { ok: false };
  if (expr.kind === ts.SyntaxKind.TrueKeyword) return { ok: true, value: true };
  if (expr.kind === ts.SyntaxKind.FalseKeyword) return { ok: true, value: false };
  if (ts.isNumericLiteral(expr)) return { ok: true, value: Number(expr.text) };
  const s = staticStringValue(expr);
  if (s !== null) return { ok: true, value: s };
  if (
    ts.isArrayLiteralExpression(expr) &&
    expr.elements.every((e) => staticStringValue(e) !== null)
  )
    return { ok: true, value: expr.elements.map(staticStringValue) };
  if (ts.isObjectLiteralExpression(expr)) return { ok: true, value: {} };
  return { ok: false };
}

/** The problem of one segment config value, or `null`. */
function segmentConfigProblem(name, node, cacheComponents) {
  if (name === 'experimental_ppr') return 'was removed in Next.js 16 (use cacheComponents)';
  if (cacheComponents && LEGACY_CACHE_CONFIG.has(name))
    return 'is removed when cacheComponents is enabled';
  if (!cacheComponents && CACHE_COMPONENTS_CONFIG.has(name))
    return 'only works when cacheComponents is enabled in next.config';
  const lit = literalOf(node);
  if (!lit.ok) return 'must be a literal value Next.js can read at build time';
  const v = lit.value;
  if (STRING_VALUES[name] && !STRING_VALUES[name].includes(v))
    return `takes ${STRING_VALUES[name].map((x) => `'${x}'`).join(' | ')}, not ${JSON.stringify(v)}`;
  if (name === 'runtime' && v === 'edge')
    return "'edge' is deprecated: remove the export (the Node.js runtime is the default)";
  if (name === 'prefetch' && v === 'auto') return "'auto' is the default: remove the export";
  if (name === 'preferredRegion') return 'is deprecated: remove the export';
  if (name === 'dynamicParams' && typeof v !== 'boolean') return 'takes true or false';
  if (name === 'revalidate' && v !== false && !(typeof v === 'number' && v >= 0))
    return 'takes false, 0 or a number of seconds';
  if (name === 'maxDuration' && !(typeof v === 'number' && v > 0))
    return 'takes a positive number of seconds';
  if (
    name === 'instant' &&
    !(typeof v === 'boolean' || (typeof v === 'object' && !Array.isArray(v)))
  )
    return 'takes true, false or an options object';
  return null;
}

function routeFileFindings(file, mod, at, cacheComponents) {
  const out = [];
  const client = mod.directive === 'use client';
  const isConfigFile = CONFIG_FILES.includes(file.special);
  for (const e of valueExports(mod.sourceFile)) {
    if (client && (file.special || file.metadata) && SERVER_ONLY_EXPORTS.includes(e.name)) {
      out.push(
        at(
          mod,
          e.node,
          'next-client-metadata',
          `"${e.name}" is only read from a Server Component, and this file is 'use client'`,
          {
            fix: "move the 'use client' part into a component the file renders",
          },
        ),
      );
    }
    if (!SEGMENT_CONFIG.has(e.name)) continue;
    if (!isConfigFile || client) {
      const where = client ? 'a Client Component' : `a ${file.special ?? 'non-route'} file`;
      out.push(
        at(
          mod,
          e.node,
          'next-segment-config-misplaced',
          `segment config "${e.name}" in ${where} is not read by Next.js`,
          {
            fix: "export it from the segment's page, layout or route handler (a Server Component)",
          },
        ),
      );
      continue;
    }
    const problem = e.value
      ? segmentConfigProblem(e.name, e.value, cacheComponents)
      : 'must be a literal value Next.js can read at build time';
    if (problem) {
      out.push(
        at(mod, e.node, 'next-segment-config-invalid', `segment config "${e.name}" ${problem}`),
      );
    }
  }
  return out;
}

function directiveFindings(mod, at) {
  const out = [];
  const isDirective = (s) =>
    ts.isExpressionStatement(s) &&
    ts.isStringLiteral(s.expression) &&
    (s.expression.text === 'use client' || s.expression.text === 'use server');
  let prologue = true;
  for (const statement of mod.sourceFile.statements) {
    const stringStatement =
      ts.isExpressionStatement(statement) && ts.isStringLiteral(statement.expression);
    if (prologue && stringStatement) continue;
    prologue = false;
    if (isDirective(statement)) {
      out.push(
        at(
          mod,
          statement,
          'next-directive-misplaced',
          `'${statement.expression.text}' after other statements is not a directive and is ignored`,
          { fix: 'move it to the very top of the file, above the imports' },
        ),
      );
    }
  }
  const visit = (node) => {
    if (ts.isFunctionLike(node) && node.body && ts.isBlock(node.body)) {
      for (const s of node.body.statements) {
        if (!(ts.isExpressionStatement(s) && ts.isStringLiteral(s.expression))) break;
        if (s.expression.text === 'use client') {
          out.push(
            at(
              mod,
              s,
              'next-directive-misplaced',
              "'use client' marks a whole module and has no effect inside a function",
              {
                fix: 'move it to the top of the file',
              },
            ),
          );
        }
      }
    }
    ts.forEachChild(node, visit);
  };
  visit(mod.sourceFile);
  return out;
}

/** Names bound to static image imports, and top-level consts built from them. */
function imageBindings(mod) {
  const images = new Set();
  for (const edge of mod.imports) {
    if (edge.typeOnly || !IMAGE_FILE.test(edge.specifier)) continue;
    for (const n of edge.names) if (n.name === 'default' && n.local) images.add(n.local);
  }
  const localPaths = new Set();
  const refersToImage = (node) => {
    let found = false;
    const visit = (n) => {
      if (found) return;
      if (ts.isIdentifier(n) && images.has(n.text)) found = true;
      else ts.forEachChild(n, visit);
    };
    visit(node);
    return found;
  };
  for (const statement of mod.sourceFile.statements) {
    if (!ts.isVariableStatement(statement)) continue;
    if (!(statement.declarationList.flags & ts.NodeFlags.Const)) continue;
    for (const decl of statement.declarationList.declarations) {
      if (!ts.isIdentifier(decl.name) || !decl.initializer) continue;
      if (refersToImage(decl.initializer)) images.add(decl.name.text);
      const s = staticStringValue(decl.initializer);
      if (s !== null && isLocalAsset(s)) localPaths.add(decl.name.text);
    }
  }
  return { images, localPaths };
}

const isLocalAsset = (value) =>
  (value.startsWith('/') && !value.startsWith('//')) ||
  value.startsWith('./') ||
  value.startsWith('../');

function rootIdentifier(node) {
  let expr = node;
  while (
    expr &&
    (ts.isPropertyAccessExpression(expr) ||
      ts.isElementAccessExpression(expr) ||
      ts.isNonNullExpression(expr) ||
      ts.isParenthesizedExpression(expr) ||
      ts.isAsExpression(expr))
  )
    expr = expr.expression;
  return expr && ts.isIdentifier(expr) ? expr.text : null;
}

function imageFindings(file, mod, at) {
  if (file && IMAGE_ROUTES.has(file.metadata)) return [];
  const out = [];
  const { images, localPaths } = imageBindings(mod);
  const insidePicture = (node) => {
    for (let p = node.parent; p; p = p.parent) {
      if (
        ts.isJsxElement(p) &&
        ts.isIdentifier(p.openingElement.tagName) &&
        p.openingElement.tagName.text === 'picture'
      )
        return true;
    }
    return false;
  };
  const visit = (node) => {
    if (
      (ts.isJsxSelfClosingElement(node) || ts.isJsxOpeningElement(node)) &&
      ts.isIdentifier(node.tagName) &&
      node.tagName.text === 'img'
    ) {
      const src = node.attributes.properties.find(
        (p) => ts.isJsxAttribute(p) && ts.isIdentifier(p.name) && p.name.text === 'src',
      );
      const init = src?.initializer;
      const expr = init && ts.isJsxExpression(init) ? init.expression : init;
      let local = false;
      if (expr) {
        const s = staticStringValue(expr);
        if (s !== null) local = isLocalAsset(s);
        else {
          const id = rootIdentifier(expr);
          local = id !== null && (images.has(id) || (ts.isIdentifier(expr) && localPaths.has(id)));
        }
      }
      if (local && !insidePicture(node)) {
        out.push(
          at(mod, node, 'next-img-element', 'a raw <img> shows a local image asset', {
            fix: "render <Image> from 'next/image' (it reserves the box and serves sized, modern formats)",
          }),
        );
      }
    }
    ts.forEachChild(node, visit);
  };
  visit(mod.sourceFile);
  return out;
}

function pagesApiFindings(mod, at) {
  return mod.imports
    .filter((e) => !e.typeOnly && PAGES_ROUTER_MODULES.has(e.specifier))
    .map((e) =>
      at(
        mod,
        e.node,
        'next-pages-api-in-app',
        `"${e.specifier}" is a Pages Router API and does nothing in the App Router`,
        {
          fix:
            e.specifier === 'next/router'
              ? "use 'next/navigation'"
              : 'use the metadata API and the root layout',
        },
      ),
    );
}

export function check({ root, options = {} }) {
  const inputs = inputsOf(root, options);
  if (!inputs.appDir) {
    if (inputs.appOption) throw new Error(`--app does not exist: ${inputs.appOption}`);
    return [];
  }
  const at = reporter(root);
  const tree = scanAppTree(inputs.appDir, inputs.extensions);
  const filesByPath = new Map(tree.files.map((f) => [f.abs, f]));
  const graph = buildModuleGraph({
    root,
    entries: appEntries(root, inputs.appDir, tree),
    options: compilerOptions(root, inputs.tsconfig),
  });
  const cacheComponents = cacheComponentsOn(root, inputs.nextConfig);
  const serverModuleRes = inputs.serverModules.map(globRe);
  const out = [];
  for (const mod of graph.values()) {
    const file = filesByPath.get(mod.file) ?? null;
    if (mod.envs.has('server')) out.push(...serverFindings(root, mod, at));
    if (mod.envs.has('client')) out.push(...clientFindings(root, mod, at, serverModuleRes));
    if (file) out.push(...routeFileFindings(file, mod, at, cacheComponents));
    out.push(
      ...directiveFindings(mod, at),
      ...imageFindings(file, mod, at),
      ...pagesApiFindings(mod, at),
    );
  }
  return out;
}

function skipIf({ root, options }) {
  const { appDir, appOption } = inputsOf(root, options);
  return appDir || appOption ? null : 'no targets: no app directory (app/ or src/app/)';
}

if (isMainModule(import.meta.url)) runScript(check, { skipIf });
