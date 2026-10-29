/**
 * Shared model of a Next.js App Router project for the `next-routes` and
 * `next-boundaries` checks:
 *
 * - the app directory tree: route segments (static, `(group)`, `[param]`,
 *   `[...param]`, `[[...param]]`, `@slot`, `(.)intercept`, `_private`) and the
 *   special files in them (`page`, `layout`, `route`, `error`, ...);
 * - the URL patterns the tree serves, and a matcher that tells whether a path
 *   (possibly with `{}` placeholders for template-literal interpolations)
 *   resolves to one of them;
 * - the module graph reached from the app's entry files, with the rendering
 *   environment of every module: `server` (Server Components, route
 *   handlers, proxy, instrumentation) and/or `client` (from a `'use client'`
 *   boundary down). A module reached through both is in both.
 *
 * Imports are resolved with the TypeScript module resolver and the project's
 * `tsconfig.json` (so path aliases such as `@/*` work); type-only imports are
 * not edges, because they are erased before bundling.
 *
 * The conventions follow the Next.js docs shipped in `next/dist/docs`
 * (`01-app/03-api-reference/03-file-conventions`).
 */

import { existsSync, readFileSync, readdirSync, statSync } from 'node:fs';
import path from 'node:path';

import { relPath } from './files.mjs';
import { ts } from './ts.mjs';

/** Default `pageExtensions` of `next.config` (the docs' routing-files table). */
export const DEFAULT_PAGE_EXTENSIONS = ['tsx', 'ts', 'jsx', 'js'];

/** Special route files that render UI and must default-export a component. */
export const COMPONENT_FILES = new Set([
  'page',
  'layout',
  'template',
  'loading',
  'error',
  'global-error',
  'not-found',
  'global-not-found',
  'default',
  'forbidden',
  'unauthorized',
]);
/** Every special route file name. */
export const SPECIAL_FILES = new Set([...COMPONENT_FILES, 'route']);
/** Metadata files that are code (each one serves a URL of its own). */
export const METADATA_CODE_FILES = new Map([
  ['icon', 'icon'],
  ['apple-icon', 'apple-icon'],
  ['opengraph-image', 'opengraph-image'],
  ['twitter-image', 'twitter-image'],
  ['sitemap', 'sitemap.xml'],
  ['robots', 'robots.txt'],
  ['manifest', 'manifest.webmanifest'],
]);
/** Static metadata files (`app/icon.svg`, `app/favicon.ico`, ...), by base name. */
const METADATA_STATIC =
  /^(?:favicon|icon\d*|apple-icon\d*|opengraph-image\d*|twitter-image\d*|sitemap|robots|manifest)\.[a-z]+$/;

/** The HTTP methods a route handler may export (route.md). */
export const HTTP_METHODS = new Set(['GET', 'HEAD', 'POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS']);

/** Route segment config exports (route-segment-config/index.md and the
 * previous caching model, caching-without-cache-components.md). */
export const SEGMENT_CONFIG = new Set([
  'dynamic',
  'dynamicParams',
  'revalidate',
  'fetchCache',
  'runtime',
  'preferredRegion',
  'maxDuration',
  'prefetch',
  'instant',
  'experimental_ppr',
]);

const SKIP_DIRS = new Set(['node_modules', '.next', '.git']);
const SOURCE_EXTENSIONS = ['.ts', '.tsx', '.mts', '.cts', '.js', '.jsx', '.mjs', '.cjs'];

/**
 * Classifies one folder name of the app directory.
 * @returns {{ kind: 'static'|'group'|'dynamic'|'catch-all'|'optional-catch-all'|'slot'|'private'|'intercept'|'invalid', value?: string, param?: string, inner?: object, reason?: string }}
 */
export function parseSegment(name) {
  if (name.startsWith('_')) return { kind: 'private' };
  if (name.startsWith('@')) {
    return /^@[\w-]+$/.test(name)
      ? { kind: 'slot', value: name.slice(1) }
      : { kind: 'invalid', reason: 'a slot name is `@` followed by letters, digits, `_` or `-`' };
  }
  const intercept = /^(\(\.\)|\(\.\.\.\)|(?:\(\.\.\))+)(.+)$/.exec(name);
  if (intercept) {
    const inner = parseSegment(intercept[2]);
    if (inner.kind === 'invalid') return inner;
    return { kind: 'intercept', value: intercept[1], inner };
  }
  if (name.startsWith('(') || name.endsWith(')')) {
    return /^\([^()[\]]+\)$/.test(name)
      ? { kind: 'group', value: name.slice(1, -1) }
      : {
          kind: 'invalid',
          reason: 'a route group is a whole folder name in parentheses: `(name)`',
        };
  }
  if (!name.includes('[') && !name.includes(']')) return { kind: 'static', value: name };
  const forms = [
    [/^\[\[\.\.\.([^[\]]+)\]\]$/, 'optional-catch-all'],
    [/^\[\.\.\.([^[\]]+)\]$/, 'catch-all'],
    [/^\[([^[\]]+)\]$/, 'dynamic'],
  ];
  for (const [re, kind] of forms) {
    const m = re.exec(name);
    if (!m) continue;
    if (m[1].startsWith('.') || /\s/.test(m[1]))
      return { kind: 'invalid', reason: `"${m[1]}" is not a parameter name` };
    return { kind, param: m[1] };
  }
  return {
    kind: 'invalid',
    reason:
      'a dynamic segment is a whole folder name: `[name]`, `[...name]` or `[[...name]]` (no text around the brackets, no `[[name]]`)',
  };
}

/** The URL-relevant view of a segment: an intercepting folder routes like its inner segment. */
function urlSegment(segment) {
  return segment.kind === 'intercept' ? segment.inner : segment;
}

/** The app directory: the `app` option, else `app`, else `src/app`; `null` when none exists. */
export function findAppDir(root, option) {
  const candidates = option ? [option] : ['app', 'src/app'];
  for (const candidate of candidates) {
    const abs = path.resolve(root, candidate);
    if (existsSync(abs) && statSync(abs).isDirectory()) return abs;
  }
  return null;
}

/**
 * Walks the app directory. Private folders (`_name`) are left out with all
 * their contents, as Next.js leaves them out of routing.
 * @returns {{ dirs: {abs:string, names:string[]}[], files: AppFile[] }}
 *
 * @typedef {{ abs: string, dir: string, names: string[], segments: object[],
 *   base: string, ext: string, special: string|null, metadata: string|null,
 *   metadataStatic: boolean }} AppFile
 */
export function scanAppTree(appDir, extensions = DEFAULT_PAGE_EXTENSIONS) {
  const dirs = [];
  const files = [];
  const stack = [{ abs: appDir, names: [] }];
  while (stack.length) {
    const dir = stack.pop();
    dirs.push(dir);
    for (const name of readdirSync(dir.abs).sort()) {
      if (SKIP_DIRS.has(name)) continue;
      const abs = path.join(dir.abs, name);
      if (statSync(abs).isDirectory()) {
        if (parseSegment(name).kind !== 'private') stack.push({ abs, names: [...dir.names, name] });
        continue;
      }
      const dot = name.indexOf('.');
      const base = dot === -1 ? name : name.slice(0, dot);
      const ext = dot === -1 ? '' : name.slice(dot + 1);
      const isCode = extensions.includes(ext);
      files.push({
        abs,
        dir: dir.abs,
        names: dir.names,
        segments: dir.names.map(parseSegment),
        base,
        ext,
        special: isCode && SPECIAL_FILES.has(base) ? base : null,
        metadata: isCode && METADATA_CODE_FILES.has(base) ? base : null,
        metadataStatic: !isCode && METADATA_STATIC.test(name),
      });
    }
  }
  const byPath = (a, b) => (a.abs < b.abs ? -1 : a.abs > b.abs ? 1 : 0);
  return { dirs: dirs.sort(byPath), files: files.sort(byPath) };
}

/**
 * The URL pattern of a file's folder chain: groups and slots take no URL
 * part; an intercepting folder is marked (it serves no URL of its own).
 * @returns {{ parts: {kind:string, value?:string, param?:string}[], intercepted: boolean, slot: boolean }}
 */
export function urlPattern(segments) {
  const parts = [];
  let intercepted = false;
  let slot = false;
  for (const segment of segments) {
    if (segment.kind === 'intercept') intercepted = true;
    if (segment.kind === 'slot') slot = true;
    const s = urlSegment(segment);
    if (s.kind === 'group' || s.kind === 'slot' || s.kind === 'invalid') continue;
    parts.push(s);
  }
  return { parts, intercepted, slot };
}

/** A canonical text of a URL pattern, the same for two folders that serve the same URLs. */
export function patternKey(parts) {
  const text = parts
    .map((p) => {
      if (p.kind === 'dynamic') return '[]';
      if (p.kind === 'catch-all') return '[...]';
      if (p.kind === 'optional-catch-all') return '[[...]]';
      return p.value;
    })
    .join('/');
  return `/${text}`;
}

/** A readable URL pattern (`/projects/[projectId]/plan`). */
export function patternText(parts) {
  return `/${parts
    .map((p) => {
      if (p.kind === 'dynamic') return `[${p.param}]`;
      if (p.kind === 'catch-all') return `[...${p.param}]`;
      if (p.kind === 'optional-catch-all') return `[[...${p.param}]]`;
      return p.value;
    })
    .join('/')}`;
}

/** The URL a metadata file serves, as pattern parts, or `null`. */
function metadataUrl(file) {
  const { parts } = urlPattern(file.segments);
  if (file.metadata)
    return [...parts, { kind: 'static', value: METADATA_CODE_FILES.get(file.metadata) }];
  if (file.metadataStatic) return [...parts, { kind: 'static', value: path.basename(file.abs) }];
  return null;
}

/**
 * Every URL the project serves: pages, route handlers, metadata routes and
 * the files of `public/`.
 * @returns {{ kind: 'page'|'route'|'metadata'|'public', parts: object[], file?: AppFile }[]}
 */
export function servedRoutes(tree, publicDir) {
  const out = [];
  for (const file of tree.files) {
    if (file.special === 'page' || file.special === 'route') {
      const { parts, intercepted } = urlPattern(file.segments);
      if (!intercepted) out.push({ kind: file.special, parts, file });
      continue;
    }
    const meta = metadataUrl(file);
    if (meta) out.push({ kind: 'metadata', parts: meta, file });
  }
  if (publicDir && existsSync(publicDir)) {
    const stack = [publicDir];
    while (stack.length) {
      const dir = stack.pop();
      for (const name of readdirSync(dir)) {
        const abs = path.join(dir, name);
        if (statSync(abs).isDirectory()) stack.push(abs);
        else {
          const rel = relPath(publicDir, abs);
          out.push({
            kind: 'public',
            parts: rel.split('/').map((value) => ({ kind: 'static', value })),
          });
        }
      }
    }
  }
  return out;
}

/**
 * Splits an internal path into its parts, or returns `null` when it is not an
 * internal path (`//host`, `https:`, `mailto:`, a bare `#anchor`, a relative
 * path, or a path whose first character is an interpolation). `{}` marks an
 * interpolation. The query and the hash take no part.
 */
export function internalPathParts(value) {
  if (typeof value !== 'string' || !value.startsWith('/') || value.startsWith('//')) return null;
  const cut = value.search(/[?#]/);
  const pathname = cut === -1 ? value : value.slice(0, cut);
  const trimmed = pathname.length > 1 ? pathname.replace(/\/+$/, '') : pathname;
  if (trimmed === '/') return [];
  return trimmed.slice(1).split('/');
}

const escapeRe = (text) => text.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');

/** Whether one href part (maybe with `{}` interpolations) can equal `value`. */
function partMatches(part, value) {
  if (!part.includes('{}')) return part === value;
  return new RegExp(`^${part.split('{}').map(escapeRe).join('.*')}$`).test(value);
}

/** Whether href parts can be a URL of the pattern `route` (`{}` matches any text). */
export function matchesPattern(route, href) {
  const go = (i, j) => {
    if (i === route.length) return j === href.length;
    const r = route[i];
    if (r.kind === 'optional-catch-all') return true;
    if (j >= href.length) return false;
    if (r.kind === 'catch-all') return true;
    if (r.kind === 'dynamic') return href[j] !== '' && go(i + 1, j + 1);
    return partMatches(href[j], r.value) && go(i + 1, j + 1);
  };
  return go(0, 0);
}

/** The routes an internal path can resolve to (`kinds` filters by route kind). */
export function resolvePath(routes, parts, kinds = null) {
  return routes.filter((r) => (!kinds || kinds.includes(r.kind)) && matchesPattern(r.parts, parts));
}

/** Parses a source file with the script kind its extension needs. */
export function parseSource(file, text) {
  const ext = path.extname(file);
  const kind =
    ext === '.tsx'
      ? ts.ScriptKind.TSX
      : ext === '.jsx'
        ? ts.ScriptKind.JSX
        : ['.js', '.mjs', '.cjs'].includes(ext)
          ? ts.ScriptKind.JS
          : ts.ScriptKind.TS;
  return ts.createSourceFile(file, text, ts.ScriptTarget.Latest, true, kind);
}

/** The module's directive prologue: `'use client'`, `'use server'` or `null`. */
export function moduleDirective(sourceFile) {
  for (const statement of sourceFile.statements) {
    if (!ts.isExpressionStatement(statement) || !ts.isStringLiteral(statement.expression)) break;
    const value = statement.expression.text;
    if (value === 'use client' || value === 'use server') return value;
  }
  return null;
}

/** Compiler options of the project's tsconfig (for module resolution only). */
export function compilerOptions(root, tsconfigOption) {
  const file = path.resolve(root, tsconfigOption ?? 'tsconfig.json');
  const fallback = {
    allowJs: true,
    jsx: ts.JsxEmit.Preserve,
    module: ts.ModuleKind.ESNext,
    moduleResolution: ts.ModuleResolutionKind.Bundler,
  };
  if (!existsSync(file)) {
    if (tsconfigOption) throw new Error(`--tsconfig does not exist: ${tsconfigOption}`);
    return fallback;
  }
  const read = ts.readConfigFile(file, ts.sys.readFile);
  if (read.error)
    throw new Error(`--tsconfig cannot be read: ${tsconfigOption ?? 'tsconfig.json'}`);
  const parsed = ts.parseJsonConfigFileContent(read.config, ts.sys, path.dirname(file));
  return { ...parsed.options, allowJs: true };
}

/** Every import-like edge of a module, with whether it is type-only. */
function importsOf(sourceFile) {
  const out = [];
  const add = (specifierNode, node, typeOnly, names) => {
    if (!specifierNode || !ts.isStringLiteralLike(specifierNode)) return;
    out.push({ specifier: specifierNode.text, node, typeOnly, names });
  };
  for (const statement of sourceFile.statements) {
    if (ts.isImportDeclaration(statement)) {
      const clause = statement.importClause;
      const names = [];
      let valueBinding = !clause;
      if (clause && !clause.isTypeOnly) {
        if (clause.name) {
          valueBinding = true;
          names.push({ name: 'default', local: clause.name.text, node: clause.name });
        }
        const bindings = clause.namedBindings;
        if (bindings && ts.isNamespaceImport(bindings)) {
          valueBinding = true;
          names.push({ name: '*', local: bindings.name.text, node: bindings.name });
        } else if (bindings) {
          for (const element of bindings.elements) {
            if (element.isTypeOnly) continue;
            valueBinding = true;
            names.push({
              name: (element.propertyName ?? element.name).text,
              local: element.name.text,
              node: element,
            });
          }
          if (bindings.elements.length === 0) valueBinding = true;
        }
      }
      add(statement.moduleSpecifier, statement, !valueBinding, names);
    } else if (ts.isExportDeclaration(statement) && statement.moduleSpecifier) {
      const clause = statement.exportClause;
      const allTypes =
        statement.isTypeOnly ||
        (clause &&
          ts.isNamedExports(clause) &&
          clause.elements.length > 0 &&
          clause.elements.every((e) => e.isTypeOnly));
      const names =
        clause && ts.isNamedExports(clause)
          ? clause.elements
              .filter((e) => !e.isTypeOnly)
              .map((e) => ({ name: (e.propertyName ?? e.name).text, local: null, node: e }))
          : [{ name: '*', local: null, node: statement }];
      add(statement.moduleSpecifier, statement, Boolean(allTypes), names);
    }
  }
  const visit = (node) => {
    if (ts.isCallExpression(node)) {
      const isDynamicImport = node.expression.kind === ts.SyntaxKind.ImportKeyword;
      const isRequire = ts.isIdentifier(node.expression) && node.expression.text === 'require';
      if ((isDynamicImport || isRequire) && node.arguments.length === 1)
        add(node.arguments[0], node, false, [{ name: '*', local: null, node }]);
    }
    if (ts.isImportTypeNode(node)) return;
    ts.forEachChild(node, visit);
  };
  ts.forEachChild(sourceFile, visit);
  return out;
}

/**
 * Builds the module graph reached from `entries` (`{ file, env }`). A module
 * with `'use client'` is client whatever imported it; a `'use server'` module
 * runs on the server. Only modules under `root` and outside `node_modules`
 * are followed.
 *
 * `via[env]` is the module that first brought a module into `env` (breadth
 * first, so the shortest import chain), `null` for an entry.
 *
 * @returns {Map<string, { file: string, sourceFile: ts.SourceFile, directive: string|null,
 *   envs: Set<string>, via: Record<string, string|null>, imports: { specifier: string, node: ts.Node, typeOnly: boolean,
 *   names: {name:string, local:string|null, node:ts.Node}[], resolved: string|null }[] }>}
 */
export function buildModuleGraph({
  root,
  entries,
  options,
  readText = (file) => readFileSync(file, 'utf8'),
}) {
  const modules = new Map();
  const cache = ts.createModuleResolutionCache(root, (x) => x, options);
  const resolve = (specifier, from) => {
    const result = ts.resolveModuleName(specifier, from, options, ts.sys, cache).resolvedModule;
    if (!result || result.isExternalLibraryImport) return null;
    const file = path.resolve(result.resolvedFileName);
    if (file.endsWith('.d.ts') || file.split(path.sep).includes('node_modules')) return null;
    if (!SOURCE_EXTENSIONS.includes(path.extname(file))) return null;
    return relPath(root, file).startsWith('..') ? null : file;
  };
  const load = (file) => {
    let mod = modules.get(file);
    if (mod) return mod;
    const sourceFile = parseSource(file, readText(file));
    const imports = importsOf(sourceFile).map((edge) => ({
      ...edge,
      resolved: resolve(edge.specifier, file),
    }));
    const directive = moduleDirective(sourceFile);
    mod = { file, sourceFile, directive, envs: new Set(), via: {}, imports };
    modules.set(file, mod);
    return mod;
  };
  const queue = entries.map((entry) => [entry.file, entry.env, null]);
  while (queue.length) {
    const [file, env, from] = queue.shift();
    const mod = load(file);
    const own =
      mod.directive === 'use client' ? 'client' : mod.directive === 'use server' ? 'server' : env;
    if (mod.envs.has(own)) continue;
    mod.envs.add(own);
    mod.via[own] = from;
    for (const edge of mod.imports) {
      if (!edge.typeOnly && edge.resolved) queue.push([edge.resolved, own, file]);
    }
  }
  return modules;
}

/** The project-level entry files beside the app directory (proxy, middleware, instrumentation). */
export function rootEntries(root, appDir) {
  const out = [];
  const bases = [path.dirname(appDir)];
  if (path.resolve(root) !== path.dirname(appDir)) bases.push(path.resolve(root));
  for (const base of bases) {
    for (const [name, env] of [
      ['proxy', 'server'],
      ['middleware', 'server'],
      ['instrumentation', 'server'],
      ['instrumentation-client', 'client'],
    ]) {
      for (const ext of ['ts', 'js', 'mjs', 'tsx', 'jsx']) {
        const file = path.join(base, `${name}.${ext}`);
        if (existsSync(file)) out.push({ file, env });
      }
    }
  }
  return out;
}

/** The graph entries of an app tree: every special and metadata code file is a server entry
 * (its own `'use client'` directive makes it client), plus the root entry files. */
export function appEntries(root, appDir, tree) {
  const entries = tree.files
    .filter((f) => f.special || f.metadata)
    .map((f) => ({ file: f.abs, env: 'server' }));
  return [...entries, ...rootEntries(root, appDir)];
}

/** Names a module exports with a value (not `export type`), each with its node. */
export function valueExports(sourceFile) {
  const out = [];
  const exported = (node) =>
    ts.canHaveModifiers(node) &&
    (ts.getModifiers(node) ?? []).some((m) => m.kind === ts.SyntaxKind.ExportKeyword);
  const isDefault = (node) =>
    (ts.getModifiers(node) ?? []).some((m) => m.kind === ts.SyntaxKind.DefaultKeyword);
  for (const statement of sourceFile.statements) {
    if (ts.isExportAssignment(statement)) {
      if (!statement.isExportEquals)
        out.push({ name: 'default', node: statement, value: statement.expression });
      continue;
    }
    if (ts.isExportDeclaration(statement)) {
      if (statement.isTypeOnly) continue;
      const clause = statement.exportClause;
      if (clause && ts.isNamedExports(clause)) {
        for (const element of clause.elements) {
          if (!element.isTypeOnly)
            out.push({ name: element.name.text, node: element, value: null });
        }
      } else if (clause && ts.isNamespaceExport(clause)) {
        out.push({ name: clause.name.text, node: statement, value: null });
      } else {
        out.push({ name: '*', node: statement, value: null });
      }
      continue;
    }
    if (!exported(statement)) continue;
    if (ts.isFunctionDeclaration(statement) || ts.isClassDeclaration(statement)) {
      out.push({
        name: isDefault(statement) ? 'default' : (statement.name?.text ?? 'default'),
        node: statement,
        value: statement,
      });
    } else if (ts.isVariableStatement(statement)) {
      for (const decl of statement.declarationList.declarations) {
        if (ts.isIdentifier(decl.name))
          out.push({ name: decl.name.text, node: decl, value: decl.initializer ?? null });
      }
    } else if (ts.isEnumDeclaration(statement)) {
      out.push({ name: statement.name.text, node: statement, value: null });
    }
  }
  return out;
}

/** Whether the JSX of a file renders the intrinsic elements `<html>` and `<body>`. */
export function rendersDocument(sourceFile) {
  const tags = new Set();
  const visit = (node) => {
    if (ts.isJsxOpeningElement(node) || ts.isJsxSelfClosingElement(node)) {
      if (ts.isIdentifier(node.tagName)) tags.add(node.tagName.text);
    }
    ts.forEachChild(node, visit);
  };
  visit(sourceFile);
  return tags.has('html') && tags.has('body');
}

/** The local names a module binds to `names` imported from `moduleName` (e.g. `next/navigation`'s `redirect`). */
export function importedLocals(mod, moduleName, names) {
  const out = new Map();
  for (const edge of mod.imports) {
    if (edge.specifier !== moduleName || edge.typeOnly) continue;
    for (const n of edge.names) if (n.local && names.includes(n.name)) out.set(n.local, n.name);
  }
  return out;
}

/** The file-relative position helper of the checks. */
export function nodePosition(sourceFile, node) {
  const { line, character } = sourceFile.getLineAndCharacterOfPosition(node.getStart(sourceFile));
  return { line: line + 1, col: character + 1 };
}
