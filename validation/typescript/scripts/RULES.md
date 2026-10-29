# Rule catalogue

Every check is `checks/<name>.mjs`. Paths in options are relative to `--root`.
Findings print as `path:line:col: rule-id: message`. See README.md for exit
codes, the runner and how to build the input files of the configurable checks.

## Summary

| Check              | Rule ids                                                                                                                                                                                                                                                                                                                                                                                               | Runs by default | Needs an input file |
| ------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | --------------- | ------------------- |
| `html-scripts`     | `html-scripts-not-deferred`, `html-scripts-third-party-origin`                                                                                                                                                                                                                                                                                                                                         | yes             | no                  |
| `i18n-catalogues`  | `i18n-catalogue-invalid-json`, `-non-string-value`, `-empty-value`, `-missing-file`, `-missing-key`, `-extra-key`, `-placeholder-mismatch`, `-placeholder-ellipsis`, `-missing-required-string`                                                                                                                                                                                                        | yes             | optional            |
| `i18n-usage`       | `i18n-usage-missing-key`, `i18n-usage-unused-key`, `i18n-usage-dynamic-key-missing-member`, `i18n-usage-missing-interpolation`                                                                                                                                                                                                                                                                         | yes             | no                  |
| `i18n-protected`   | `i18n-protected-code-not-verbatim`, `i18n-protected-namespace-missing`                                                                                                                                                                                                                                                                                                                                 | yes             | no                  |
| `tokens`           | `tokens-undeclared-reference`, `tokens-missing`, `tokens-value-mismatch`                                                                                                                                                                                                                                                                                                                               | yes             | optional            |
| `contrast`         | `contrast-below-aa`                                                                                                                                                                                                                                                                                                                                                                                    | yes             | optional            |
| `css-usage`        | `unused-css-class`                                                                                                                                                                                                                                                                                                                                                                                     | yes             | no                  |
| `dist-policy`      | `dist-policy-router-devtools`, `dist-policy-msw`, plus rule ids from `--banned`                                                                                                                                                                                                                                                                                                                        | no              | optional            |
| `bundle-size`      | `size-limit-exceeded`, `build-failed`                                                                                                                                                                                                                                                                                                                                                                  | no              | no (budgets)        |
| `api-types-only`   | `api-types-only-extra-file`, `api-types-only-unexpected-export`                                                                                                                                                                                                                                                                                                                                        | no              | no                  |
| `api-contract-map` | `api-contract-map-no-match`, `api-contract-map-ambiguous`                                                                                                                                                                                                                                                                                                                                              | no              | yes                 |
| `nav-tree`         | `nav-tree-missing`, `-missing-entry`, `-unlisted-entry`, `-entry-mismatch`, `-role-restriction-missing`, `-role-restriction-excess`                                                                                                                                                                                                                                                                    | no              | yes                 |
| `next-routes`      | `next-route-segment-invalid`, `-catch-all-not-last`, `-param-duplicate`, `-slug-conflict`, `-duplicate`, `-page-and-handler`, `-root-layout-missing`, `-document-tags`, `-global-misplaced`, `-default-export`, `-handler-export`, `-error-not-client`, `-dynamic-fallback`, `next-link-unresolved`, `next-anchor-internal`, `next-route-table-unresolved`, `next-nav-unresolved`, `next-nav-unrouted` | no              | optional            |
| `next-boundaries`  | `next-client-api-in-server`, `next-server-api-in-client`, `next-server-env-in-client`, `next-client-metadata`, `next-segment-config-invalid`, `next-segment-config-misplaced`, `next-directive-misplaced`, `next-img-element`, `next-pages-api-in-app`                                                                                                                                                 | no              | no                  |
| `semgrep`          | the last dotted segment of each Semgrep `check_id`                                                                                                                                                                                                                                                                                                                                                     | no              | rules directory     |
| duplicates         | `jscpd-clone`                                                                                                                                                                                                                                                                                                                                                                                          | no (tool step)  | no                  |

Tool steps (runner `tools` list or `--only`): `tsc`, `eslint`, `knip`,
`knip-production` (`knip --production`: code and dependencies used only by
tests or dev tooling), `prettier`, `stylelint`, `duplicates`. Rule ids are the
tools' own.

## html-scripts

Options: `--html <file>` (default `index.html`). Skips when the file is absent.
The reader is a small tag-attribute scanner, not a full HTML parser; it ignores
comments.

- `html-scripts-not-deferred`: every `<script>` is `type="module"`, `defer` or
  `async`. Why: a classic script blocks HTML parsing and delays first paint.
- `html-scripts-third-party-origin`: no tag has a `src` or `href` that is an
  absolute `http(s)://` URL or a protocol-relative `//host` reference. Why:
  first paint should not wait on another origin's stylesheet, font or script;
  ship those resources with the built assets.

```html
<!-- bad -->
<script src="/app.js"></script>
<link rel="stylesheet" href="https://cdn.example.com/a.css" />
<!-- good -->
<script type="module" src="/app.js"></script>
```

## i18n-catalogues

Options: `--locales <dir>` (default `src/locales`), `--base-locale <code>`
(default `en`; the first locale alphabetically if absent), `--required-strings
<file>` (optional JSON). Reads `<locales>/<locale>/**/*.json`; locales are the
sub-directories. Skips when the directory is missing or holds fewer than two
locales. Why: a key or placeholder present in one locale and not another
renders a raw key or a literal `{{name}}` at runtime, and only for some users.

- `i18n-catalogue-invalid-json`: a file does not parse.
- `i18n-catalogue-non-string-value`: a value is not a flat string (nested
  objects are flattened to dotted keys; arrays and numbers are rejected).
- `i18n-catalogue-empty-value`: a value is an empty or blank string.
- `i18n-catalogue-missing-file`: a locale lacks a file the base has.
- `i18n-catalogue-missing-key`: a locale lacks a key the base has.
- `i18n-catalogue-extra-key`: a locale has a key the base lacks.
- `i18n-catalogue-placeholder-mismatch`: a key's `{{placeholder}}` set differs
  from the base's.
- `i18n-catalogue-placeholder-ellipsis`: a key whose last segment ends in
  `placeholder` (an input hint) ends with `...` instead of the single
  character U+2026. A key with `placeholder` in a middle segment is not
  a hint. On by default; `--placeholder-ellipsis false` turns the rule off
  and keeps key and placeholder parity.
- `i18n-catalogue-missing-required-string` (configurable): with
  `--required-strings`, a string listed there appears as no value (trimmed, exact
  text) of the base-locale catalogue. Why: user-facing messages that a
  specification fixes word for word (an empty-state or error message) must not
  drift. Input: `{ "strings": [ { "value": "...", "label": "..." } ] }`, or a
  plain array of strings or `{ value }` objects; the `label` only appears in
  the message. See `examples/required-strings.example.json`.

```json
// en/common.json
{ "greeting": "Hello {{name}}", "search.placeholder": "Search…" }
// pt-BR/common.json  (bad: placeholder renamed, three dots)
{ "greeting": "Olá {{nome}}", "search.placeholder": "Buscar..." }
```

## i18n-usage

Options: `--src <dir>` (default `src`), `--locales <dir>`, `--base-locale
<code>`. Written for i18next-style `t(key, options)` calls. Skips when the
source or catalogue directory is absent. Compares code against the base
catalogue only.

- `i18n-usage-missing-key`: `t("a.b")` names a key absent from the base
  catalogue. A key with no entry of its own but with plural forms
  (`<key>_one`, `<key>_other`, ...) counts as present.
- `i18n-usage-unused-key`: a catalogue key is never used. A key counts as used
  when a call names it, when a dynamic call's prefix matches it, or when its
  exact text appears as a string literal anywhere in the source (so keys
  passed through props or config objects are not reported).
- `i18n-usage-dynamic-key-missing-member`: for ``t(`p.${v}`)`` where the type
  of `v` expands to a finite literal union, every `p.<member>` must exist.
  Resolved with a real `ts.Program`, using the target's `tsconfig.json` when
  present. A type that cannot be expanded (plain `string`) is not judged and
  its prefix counts as used.
- `i18n-usage-missing-interpolation`: a `{{name}}` in the key's value gets no
  value at the call. Names come from the options object literal, or from its
  type when spread or passed by reference; an index signature or `any` is not
  judged.

Why: a missing key shows the raw key to users, an unused key is dead
translation work, and a missing interpolation shows `{{name}}` literally.

```ts
// bad: 'order.total' has {{amount}} but no value is passed
t('order.total');
// good
t('order.total', { amount });
```

## i18n-protected

Options: `--src`, `--locales`, `--base-locale`, `--localised-code-families
<prefix,...>` (default none), `--namespaces <ns,...>` (default none). A key is
protected exactly when it is a resolved dynamic call site and its base value
equals that literal member verbatim (`status.NEW` is `"NEW"`). It then must keep
that value in every other locale. A dynamic key whose base value is a human
label (`status.band_HIGH` is `"High"`) is not protected and may be translated.
Families named in `--localised-code-families` are never protected.

- `i18n-protected-code-not-verbatim`: a locale translated a protected code.
- `i18n-protected-namespace-missing` (configurable): a namespace listed in
  `--namespaces` has no `<namespace>.*` key in the base catalogue. Why: a
  documented key namespace that the catalogue never provides means a feature's
  strings are missing altogether.

Why: codes that appear in data, logs or documentation must read the same in
every language, or users cannot match what they see to what they report.

## tokens

Options: `--src <dir>` (default `src`), `--tokens-css <file>` (default
`src/styles/tokens.css`), `--tokens-spec <file>` (optional JSON). Skips when
the theme stylesheet is absent. Scans `.ts`, `.tsx` and `.css` files under
`--src`.

- `tokens-undeclared-reference`: `var(--x)` without a fallback names a custom
  property that the `:root` block of the theme stylesheet does not declare.
  Reported at the referencing line. A reference with a fallback
  (`var(--cols, 1)`) is ignored, and so is a `var(--x)` inside a CSS
  `/* ... */` comment, which renders nothing.
- `tokens-missing` (configurable): with `--tokens-spec`, a token listed there is
  not declared in `:root`.
- `tokens-value-mismatch` (configurable): a listed token that gives a `value`
  is declared with a different one (whitespace collapsed). Input: `{ "tokens":
[ { "name": "--background", "value": "oklch(0.97 0 0)" }, { "name": "--accent" } ] }`;
  see `examples/tokens.spec.example.json`. Why: the stylesheet is the
  implementation of a declared palette, and the two must not drift.

Why: CSS resolves an undeclared variable to nothing without any error, so the
declaration silently renders as unset. Checking against `:root` only, not
"declared anywhere", stops components minting private tokens beside the
palette.

```css
/* bad */
.card {
  color: var(--text-primay);
}
/* good */
.card {
  color: var(--text-primary);
}
```

## contrast

Options: `--tokens-css <file>` (default `src/styles/tokens.css`), `--min-ratio
<n>` (default `4.5`, at least `1`), `--pairs <file>` (JSON
`{ "pairs": [["--bg", "--fg"], ...] }`, see `examples/contrast-pairs.example.json`).
Skips when the stylesheet is absent, or when no pair can be formed (never a
silent clean pass). Without `--pairs`, pairs come from the `:root` names:
`--foreground` on `--background`, and each `--x-foreground` on `--x` when `--x`
is declared; `--pairs` replaces them for any other naming convention, and a
pair naming an undeclared token exits `2`. Only `oklch()` values are
evaluated; others are skipped, never guessed. Colours convert from OKLCH to
sRGB and the WCAG 2.1 relative-luminance ratio is computed.

- `contrast-below-aa`: a pair is below the minimum ratio. The message states
  the pair and the measured ratio.

Why: text below 4.5:1 is hard to read for many users with low vision.

```css
:root {
  --primary: oklch(0.7 0 0);
  --primary-foreground: oklch(0.75 0 0); /* bad: about 1.2:1 */
}
```

## css-usage

Options: `--src <dir>` (default `src`), `--html <file>` (default
`index.html`), `--css <glob>` (default `src/**/*.css`). Evidence of use is the
HTML file plus `.ts`/`.tsx` under `--src`, excluding `*.test.*` and `*.spec.*`.
PurgeCSS (`rejected: true`) finds unused selectors without rewriting
styles; `postcss-selector-parser` keeps only selectors with a class. Type and
pseudo selectors are never reported. Globs are made absolute, so the result
does not depend on the working directory. Skips when no stylesheet matches
`--css` or the `--src` directory does not exist.

- `unused-css-class`: a class selector has no usage evidence in production
  sources.

Why: dead styles grow the bundle and hide which rules still matter; a class
referenced only by a test is not production use.

## dist-policy

Options: `--assets <dir>` (default `dist/assets`), `--banned <file>` (JSON
array of `{ rule, label, markers: [..] }`, see
`examples/dist-policy.example.json`; entries extend the two built-in ones, an
entry with a built-in `rule` replaces it), `--banned-replace` (use only the
file's entries). Scans built `.js` files
recursively for each library's own literal strings, which survive
minification. Exits `2` when the directory is missing: the script never builds,
and a missing build is not a pass.

- `dist-policy-router-devtools`: markers `router-devtools` or
  `TanStackRouterDevtools`.
- `dist-policy-msw`: markers `Mock Service Worker` or `mockServiceWorker.js`.
- Any rule id from `--banned`: the entry's markers.

Why: development-only libraries are normally gated behind a dev flag that the
bundler removes in production. This guards against that gate silently
breaking and shipping the tool to users.

## bundle-size

Options: `--dist <dir>` (default `dist`), `--html <file>` (default
`index.html`, relative to `--dist`), `--entry-budget <bytes>`, `--total-budget
<bytes>`, `--build <command>`. Budgets are positive integers. Sizes are measured
by size-limit (Brotli, the `@size-limit/file` default, pinned in the kit).

Two modes. With a budget given, the kit writes a temporary size-limit
configuration itself: the entry payload is the scripts and module preloads that
the built HTML loads up front (third-party URLs excluded; each must be local,
inside the build directory and present), and the total payload is every
`.js`/`.mjs` file under the build directory. With no budget, size-limit runs in
the root with the target's own configuration (`.size-limit.*` or the
`size-limit` key of `package.json`). `--build` runs a command through the shell
in the root before measuring.

- `size-limit-exceeded`: a measured payload is over its budget; the message
  states size and limit.
- `build-failed`: the `--build` command failed.

A missing build directory, a missing size-limit, or size-limit's own error or
non-JSON output exits `2`, never a finding and never a pass.

Why: a page's first load grows silently with every dependency; a byte budget on
the entry and total payload makes the growth a visible, reviewable change.

## api-types-only

Options: `--generated-dir <dir>` (default `src/generated`), `--expected-file
<name>` (default `api.d.ts`). Exits `2` when the directory is missing. Export
names are read with the TypeScript compiler API.
`openapi-typescript` produces exactly five top-level exports: `paths`,
`webhooks`, `components`, `operations`, `$defs`.

- `api-types-only-extra-file`: any `.ts` or `.d.ts` file other than the
  expected one in the generated directory (searched recursively).
- `api-types-only-unexpected-export`: the generated file exports a name outside
  the five.

Why: a generated directory is overwritten by the generator. A hand-written
type placed there is lost on the next run, or hides a stale generation.

```ts
// api.d.ts
export interface paths {} // ok
export interface ExtraHeaderProps {} // bad: not generator output
```

## api-contract-map (configurable)

Options: `--src <dir>` (default `src`), `--contracts <file>` (default
`api-contracts.json`). Input: the endpoint list of your API.

```json
{
  "callees": { "httpGet": "GET", "httpPost": "POST" },
  "endpoints": [{ "id": "orders.get", "method": "GET", "path": "/orders/{id}" }]
}
```

`callees` is optional (default `httpGet`, `httpPost`, `httpPut`, `httpPatch`,
`httpDelete`) and maps the name of a client function to its HTTP method. Build
the file from an OpenAPI document with `tools/openapi-to-contracts.mjs` (see
README.md) or write it by hand; see `examples/api-contracts.example.json`.

The check reads every call to a configured callee under `--src` (test files
excluded) with the TypeScript compiler API and matches its method and
normalised path structurally against the endpoints: a `{param}` segment matches
any one path segment whatever its name, and a query string takes no part. A call
whose path is not a static string or template literal is skipped, never guessed.

- `api-contract-map-no-match`: the call matches no endpoint.
- `api-contract-map-ambiguous`: the call matches endpoints with more than one
  distinct id.

Why: a client path typed by hand drifts from the server contract and fails only
at runtime; mapping each call to exactly one declared contract catches renamed,
removed and ambiguous endpoints in the build.

## nav-tree (configurable)

Options: `--src <dir>` (default `src`), `--nav-spec <file>` (default
`nav-spec.json`). Input: the declared navigation tree; see
`examples/nav-spec.example.json`.

| Key                | Meaning                                                                                                                                                        |
| ------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `variable`         | Name of the top-level `const` array in code (default `NAV_ITEMS`)                                                                                              |
| `fields`           | Property names on a code entry: `key`, `path`, `icon`, `exact`, `children`, `restricted` (defaults `labelKey`, `to`, `icon`, `exact`, `children`, `adminOnly`) |
| `items`            | The tree: `{ label, path?, icon?, exact?, key?, group?, children? }`; `label` is required and only appears in messages                                         |
| `restrictedRoutes` | Optional map of route path to boolean: whether the entry must carry the restriction flag                                                                       |

The code side is the first top-level `const <variable> = [ ... ]` under `--src`
whose elements are all object literals carrying the `key` field.

- `nav-tree-missing`: no navigation array was found under the source root.
- `nav-tree-missing-entry`: a declared entry has no code entry at its position.
- `nav-tree-unlisted-entry`: a code entry has no declared entry at its position.
- `nav-tree-entry-mismatch`: path, `exact`, icon, key or group-ness differ (the
  key is compared only when the declared entry gives one).
- `nav-tree-role-restriction-missing` / `nav-tree-role-restriction-excess`: for
  every code entry whose path is in `restrictedRoutes`, the effective
  restriction (its own flag, or an ancestor group's) must equal the listed
  value. Without `restrictedRoutes` this half is off.

Why: navigation is the user's map of the product; a menu entry missing, extra,
or visible to a user who cannot open its page is a defect no type check sees.

## next-routes (Next.js App Router)

Options: `--app <dir>` (default `app`, else `src/app`; a named directory that
does not exist exits `2`), `--public <dir>` (default `public`),
`--page-extensions <list>` (default `tsx,ts,jsx,js`, as `pageExtensions` in
`next.config`), `--tsconfig <file>` (default `tsconfig.json`; its `paths`
resolve aliases such as `@/*`). Skips when there is no app directory.

The conventions are those of the Next.js docs that ship with the installed
version (`node_modules/next/dist/docs/01-app/03-api-reference/03-file-conventions`).
Folder names are parsed as segments: static, `(group)`, `[param]`,
`[...param]`, `[[...param]]`, `@slot`, `(.)` / `(..)` / `(...)` intercepting
and `_private` (left out of routing with all its contents). A URL is served by
a `page` or a `route` file; groups and slots take no URL part, and an
intercepting folder serves no URL of its own.

Route tree:

- `next-route-segment-invalid`: a folder name is not a valid segment (text
  around brackets, `[[name]]`, an empty or dotted parameter, an unclosed group,
  an empty slot name).
- `next-route-catch-all-not-last`: a catch-all segment has URL segments below it.
- `next-route-param-duplicate`: one URL uses a parameter name twice.
- `next-route-slug-conflict`: two dynamic segments at the same URL position
  differ in name or kind (Next.js refuses to build).
- `next-route-duplicate`: two pages, or two route handlers, serve the same URL
  (typically through two route groups).
- `next-route-page-and-handler`: a page and a route handler serve the same URL.
- `next-route-root-layout-missing`: no root layout is above a page
  (`app/layout`, or a layout in a top-level route group).
- `next-route-document-tags`: a root layout, `global-error` or
  `global-not-found` does not render `<html>` and `<body>`.
- `next-route-global-misplaced`: `global-error` / `global-not-found` is not at
  the root of the app directory, the only place Next.js reads it.
- `next-route-default-export`: a `page`, `layout`, `template`, `loading`,
  `error`, `global-error`, `not-found`, `global-not-found`, `default`,
  `forbidden` or `unauthorized` file has no default export.
- `next-route-handler-export`: a route handler has a default export, re-exports
  a whole module, or exports a value that is not an HTTP method (`GET`, `HEAD`,
  `POST`, `PUT`, `PATCH`, `DELETE`, `OPTIONS`), route segment config or
  `generateStaticParams`. Type exports are free.
- `next-route-error-not-client`: an `error` / `global-error` boundary is not a
  Client Component (`'use client'`).
- `next-route-dynamic-fallback`: a page under a dynamic segment has no strategy
  for a parameter value that names nothing. Any one of these counts: an
  `error`, `not-found` or `global-error` file at or above the page's folder; a
  call of `notFound()`, `redirect()`, `permanentRedirect()`, `forbidden()` or
  `unauthorized()` (from `next/navigation`) in the page or a layout above it;
  `generateStaticParams` in the page with `dynamicParams = false` in the page
  or a layout above it.

Navigation targets are read from the modules reached from the app's entry
files (tests and scripts are not application code). A target is the static
value (string or template literal) given to `<Link href>` (the default import
of `next/link`), `<a href>`, `redirect()` / `permanentRedirect()` and the
`push` / `replace` / `prefetch` methods of a `useRouter()` router. Only paths
that start with one `/` are internal; the query and hash take no part, and a
template interpolation matches any text.

- `next-link-unresolved`: an internal target resolves to no page, route
  handler, metadata route (`icon.svg`, `sitemap.ts`, ...) or `public/` file.
- `next-anchor-internal`: a raw `<a href>` with a static path to a page: it
  reloads the whole document; render `<Link>`. A `download` link is not
  navigation.

Route table and navigation (optional; turn them on with the options below):

| Option                    | Meaning                                                                                                                                       |
| ------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------- |
| `route-table`             | `<file>#<ConstName>`: a top-level `const` object whose properties are route templates, or objects holding one                                 |
| `route-table-template`    | The property holding the template in an object entry (default `template`). Parameters: `{id}`, `[id]`, `:id` or `$id`                         |
| `route-table-prefixes`    | `<file>#<ConstName>`: a `const` object of named path prefixes (for example a demo tree under `/demo`); without it the templates are the paths |
| `route-table-prefix-keys` | The property of an entry that lists the prefix names it exists under; without it every prefix applies                                         |
| `nav-spec`                | The `nav-tree` input (`examples/nav-spec.example.json`): every entry's `path` must resolve to a page                                          |
| `nav-route-key`           | `true`: every nav entry's `key` names a route-table entry (needs `route-table`)                                                               |

- `next-route-table-unresolved`: a route-table entry, under one of its
  prefixes, resolves to no page.
- `next-nav-unresolved`: a nav entry's `path` resolves to no page.
- `next-nav-unrouted`: a nav entry's `key` names no route-table entry.

Why: a route that Next.js cannot build, a page without a root document, an
error boundary that cannot catch, or a link to a page that is gone each fail
only at build or run time, often for one URL only; the file system is the
router, so the tree itself can be checked.

## next-boundaries (Next.js App Router)

Options: `--app`, `--page-extensions`, `--tsconfig` (as `next-routes`),
`--server-modules <globs>` (file-name globs of modules that must stay on the
server, default `*.server.*`), `--next-config <file>` (default the first
`next.config.{ts,mjs,js,cjs,mts}`; a named file that does not exist exits `2`).
Skips when there is no app directory.

The module graph starts at the app's special and metadata files and at
`proxy`, `middleware`, `instrumentation` (server) and `instrumentation-client`
(client) beside the app directory. Imports are resolved with the TypeScript
resolver and `tsconfig.json`; type-only imports are erased and are no edge. A
module with `'use client'` is client whatever imported it, and so is everything
it imports; a `'use server'` module runs on the server. A module reached from
both sides is checked as both. Messages name the module that brought a module
into its graph.

- `next-client-api-in-server`: a module in the server graph imports a
  client-only API: `Component`, `PureComponent`, `createContext`,
  `createFactory`, `useState`, `useEffect`, `useLayoutEffect`,
  `useInsertionEffect`, `useReducer`, `useRef`, `useImperativeHandle`,
  `useSyncExternalStore`, `useTransition`, `useDeferredValue`,
  `useOptimistic`, `useActionState`, `useEffectEvent` (`react`); `flushSync`,
  `unstable_batchedUpdates`, `useFormStatus`, `useFormState` (`react-dom`);
  `useRouter`, `usePathname`, `useSearchParams`, `useParams`,
  `useSelectedLayoutSegment(s)`, `useServerInsertedHTML`, `useLinkStatus`
  (`next/navigation`); the `client-only` or `react-dom/client` modules; or
  calls `next/dynamic` with `ssr: false`. In a module only the server graph
  reaches, also: an event handler (`onClick={...}`) on a DOM element, and a
  read of `window`, `document`, `localStorage`, `sessionStorage` or
  `navigator` that is not a `typeof` guard or a local binding.
- `next-server-api-in-client`: a module in the client graph imports
  `server-only`, `next/headers`, `next/server`, a server function of
  `next/cache` (`unstable_cache`, `revalidatePath`, `revalidateTag`,
  `updateTag`, `refresh`, `cacheLife`, `cacheTag`), a Node.js built-in, or a
  module whose file name matches `--server-modules`.
- `next-server-env-in-client`: a module in the client graph reads
  `process.env.NAME` that is not `NEXT_PUBLIC_*` (or `NODE_ENV`): Next.js
  replaces it with an empty string in the browser; or reads `process.env`
  dynamically (`process.env[name]`, a bare `process.env`), which is never
  inlined.
- `next-client-metadata`: a `'use client'` route file exports `metadata`,
  `generateMetadata`, `viewport`, `generateViewport` or `generateStaticParams`,
  which Next.js reads only from Server Components.
- `next-segment-config-invalid`: a segment config export of a page, layout or
  route handler is not a literal; or `dynamic` is not `'auto'`,
  `'force-dynamic'`, `'error'` or `'force-static'`; `fetchCache` is not one of
  its seven values; `dynamicParams` is not a boolean; `revalidate` is not
  `false` or a number `>= 0`; `maxDuration` is not a positive number;
  `instant` is not a boolean or an object; `prefetch` is not `'partial'` or
  `'force-disabled'`; `runtime` is `'edge'` (deprecated) or another value but
  `'nodejs'`; `preferredRegion` is set (deprecated); `experimental_ppr` is set
  (removed in Next.js 16). With `cacheComponents: true` in `next.config`,
  `dynamic`, `dynamicParams`, `revalidate` and `fetchCache` are removed;
  without it, `prefetch` and `instant` do nothing.
- `next-segment-config-misplaced`: segment config exported from an
  app-directory file other than a page, layout or route handler, or from a
  Client Component: Next.js does not read it there.
- `next-directive-misplaced`: a `'use client'` / `'use server'` string after
  the directive prologue (it is a plain expression, the module stays what it
  was), or a `'use client'` inside a function.
- `next-img-element`: a raw `<img>` whose source the check can see is a local
  asset: a static image import (`import pic from './pic.png'`), a value read
  from it or from a top-level `const` built from it, a string literal path
  (`/logo.png`, `./a.svg`), or a `const` holding one. `next/image` reserves the
  image box and serves sized, modern formats. A source computed at run time
  (a prop, a data URL, user content) is not a local asset the check can prove.
  Inside `<picture>` and in image metadata routes (`opengraph-image`,
  `twitter-image`, `icon`, `apple-icon`) the raw element is the convention.
- `next-pages-api-in-app`: an App Router module imports `next/router`,
  `next/head` or `next/document`, which belong to the Pages Router.

Why: the boundary decides what runs where. A hook in the server graph or a
server module in the browser bundle fails the build at best, and at worst
ships a secret or an empty environment value to the browser; segment config
that Next.js ignores silently changes caching and rendering.

## semgrep

Options: `--config <dir or file>` (default `.semgrep`), `--target <dir>`
(default `src`). Paths are resolved against `--root`. The kit's TypeScript rule
set is the lint kit's `semgrep/` folder (`typescript/lint/semgrep`); point
`--config` at it, or copy it to `.semgrep` in the target. The binary is `$SEMGREP_BIN`, else the track's `.venv/bin/semgrep` (`uv sync`;
pinned in `pyproject.toml`), else `semgrep` on `PATH`. Runs with
metrics and the version check off, no timeout and JSON output. Skips when the
default config or target is absent; a `--config` or `--target` that is named
and absent exits `2`. The rule id is the last dotted segment of the
Semgrep `check_id`; the message is its first line.

It refuses to certify a tree it did not fully scan. Each of these exits `2`:
a rule that fails to load (it would silence its file-mates), a partial scan
(timeout, too big, syntax error), output that is not JSON, a Semgrep exit code
other than `0` or `1`, and a run with no scanned files.

Why: a scanner that fails open turns a broken rule set into a clean result.

## duplicates

Run from the git work tree to scan; `--project <dir>` limits it to one
directory inside the tree. Rule `jscpd-clone`: `file:line:1: jscpd-clone:
duplicates other:line`. Exits `2` when no supported source was scanned or the
project directory does not exist. Why: copied code drifts apart, so a fix lands
in one copy and not the other.

Configuration, read from `.jscpd.json` in the current directory, else the
kit's `jscpd.json`:

| Key         | Kit default                                                                                                                      |
| ----------- | -------------------------------------------------------------------------------------------------------------------------------- |
| `minLines`  | `10`                                                                                                                             |
| `minTokens` | `100`                                                                                                                            |
| `mode`      | `weak`                                                                                                                           |
| `formats`   | `typescript`, `tsx`, `javascript`, `css`; add `python`, `sql`, `bash` or `cypher` to compare those                               |
| `ignore`    | `**/fixtures/**`, `**/node_modules/**`, `**/dist/**`, `**/build/**`, `**/coverage/**`, `**/*.generated.*`, `**/src/generated/**` |

What is blanked before comparing (line numbers are kept):

- import declarations, which are naturally alike;
- test assertions (`expect(...)`, `assert(...)`) whose arguments only observe a
  value, and `waitFor` callbacks made up of only such assertions;
- the boilerplate of a typed ESLint flat config.

Blanking applies to JS/TS test files only. Files of other formats are compared
in full: when `python` is added to `formats`, Python test assertions are not
blanked.

Actions evaluated inside an assertion, rule implementations, output parsers and
fixtures remain compared. Set `VALIDATION_REPORTS_DIR` to also write a JSON
report there.

## Configurable rules

Checks whose logic is generic but whose input is your project's facts. Each
reads a JSON file you supply; neutral examples are in `examples/`.

| Check or rule                                      | Option                             | Example                                  | Built by                                       |
| -------------------------------------------------- | ---------------------------------- | ---------------------------------------- | ---------------------------------------------- |
| `i18n-catalogue-missing-required-string`           | `--required-strings`               | `examples/required-strings.example.json` | by hand                                        |
| `i18n-protected-namespace-missing`                 | `--namespaces`                     | none (a comma-separated list)            | by hand                                        |
| `tokens-missing`, `tokens-value-mismatch`          | `--tokens-spec`                    | `examples/tokens.spec.example.json`      | by hand or exported from a design-token source |
| `api-contract-map-*`                               | `--contracts`                      | `examples/api-contracts.example.json`    | `tools/openapi-to-contracts.mjs`               |
| `nav-tree-*`                                       | `--nav-spec`                       | `examples/nav-spec.example.json`         | by hand                                        |
| `next-route-table-unresolved`, `next-nav-*`        | `--route-table`, `--nav-spec`      | `examples/nav-spec.example.json`         | the route table is your own path module        |
| `dist-policy` (extra dev-only libraries)           | `--banned`                         | `examples/dist-policy.example.json`      | by hand                                        |
| `contrast` (other token naming)                    | `--pairs`                          | `examples/contrast-pairs.example.json`   | by hand                                        |
| `i18n-catalogue-placeholder-ellipsis` (switch off) | `--placeholder-ellipsis`           | none (`true` or `false`)                 | choose                                         |
| `bundle-size`                                      | `--entry-budget`, `--total-budget` | none                                     | choose the byte budgets                        |

Two further checks are specified but not shipped as code, because their input
is a component catalogue or a set of state components that each project writes
in its own way. Build them from the specs:

- [`schema-specific/primitives.md`](schema-specific/primitives.md): a shared UI
  component catalogue. Each listed component is exported exactly once, built on
  the configured design-system package, and not shadowed or re-implemented as a
  local variant (`primitives-*`).
- [`schema-specific/state-surfaces.md`](schema-specific/state-surfaces.md):
  data-backed surfaces render loading, empty and error states, query hooks handle
  their configured errors, and required empty-state strings are rendered through
  the empty-state component with an action where needed (`state-surfaces-*`).

## Shared helpers (`checks/lib/`)

| File                 | Role                                                                                                                                         |
| -------------------- | -------------------------------------------------------------------------------------------------------------------------------------------- |
| `main.mjs`           | CLI bootstrap: `--root`, option collection, `skipIf` (exit 0, `skipped:` on stderr), an exception gives exit 2, async checks, no positionals |
| `report.mjs`         | `finding`, `report`, `skip`, `toolError`: the output contract                                                                                |
| `files.mjs`          | `parseArgs`, `listFiles`, `relPath`                                                                                                          |
| `bin.mjs`            | Locates a CLI binary in `node_modules/.bin` at or above a directory; the kit folder is always a search root                                  |
| `parsers.mjs`        | Parsers for `tsc`, ESLint JSON, knip JSON, Node test and Prettier output, and the finding contract; `renderFinding`                          |
| `spec-input.mjs`     | Loader for adopter JSON inputs; a missing or malformed file is a configuration error (exit 2)                                                |
| `payload-budget.mjs` | Entry and total JavaScript discovery, budget parsing, size-limit configuration and report interpretation                                     |
| `api-calls.mjs`      | Finds client calls and matches a call path to an endpoint path                                                                               |
| `program.mjs`        | Builds a `ts.Program` from the target's `tsconfig.json`, or from the source tree when there is none                                          |
| `ts.mjs`             | TypeScript AST helpers                                                                                                                       |
| `i18n.mjs`           | Catalogue reader: locale discovery, flattening, key locations, base-locale choice                                                            |
| `i18n-calls.mjs`     | Finds `t()` calls and expands dynamic keys to literal unions                                                                                 |
| `tokens-css.mjs`     | Reads the `:root` custom properties of a stylesheet                                                                                          |
| `oklch.mjs`          | OKLCH to sRGB conversion and WCAG contrast ratio                                                                                             |

## Configuration choices

- `validation.config.example.json`: lists each check with example options, the
  optional `tools` and `binDirs`. Copy it and edit paths.
- `jscpd.json`: clone-detection defaults, described above.
- `.prettierrc.json`: single quotes, semicolons, trailing commas, 100
  columns, LF.
- `package.json`: exact dependency versions, `"type": "module"`, Node
  `>=24`.
- `pyproject.toml` and `uv.lock`: the pinned Semgrep version.
