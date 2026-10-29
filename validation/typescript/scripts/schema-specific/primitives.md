# Spec: `primitives` (shared UI component catalogue)

Status: specification only. The kit does not ship this check as code, because
its input is a component catalogue that each project writes in its own way.
Build it from this spec once you have such a catalogue. Use the shared helpers
in `checks/lib/` (`ts.mjs`, `files.mjs`, `report.mjs`, `main.mjs`,
`spec-input.mjs`) and the same CLI contract as every other check: exit `0`
clean, `1` findings, `2` configuration or tool error, and `skipped:` on stderr
when there is nothing to check.

## What it enforces

A front end often has a small set of shared UI components (a status pill, a
page header, a skeleton). Each one should be implemented once, in its own
module, and built on the design-system package the project uses. The check
looks for these defects:

| Rule                                 | Defect                                                                                                                                                                                                             |
| ------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `primitives-missing`                 | A listed component is exported by no module under the source root.                                                                                                                                                 |
| `primitives-duplicate-export`        | A listed component is exported by more than one module.                                                                                                                                                            |
| `primitives-not-composed`            | Neither the module that exports a listed component nor any module it reaches through at most `maxCompositionDepth` relative imports imports the design-system package. Exempt components are skipped.              |
| `primitives-name-shadow`             | A component-shaped top-level declaration, exported or not, uses a listed component's name outside that component's module directory. This is a second implementation under the same name.                          |
| `primitives-duplicate-local-variant` | Component-shaped declarations whose lower-cased name contains a variant category word (for example `pill`, `badge`, `skeleton`) exist outside the allowed names. One finding per category, listing every offender. |

A declaration is component-shaped when it has a PascalCase name and is bound to
a function. That covers a function declaration, and a `const` whose initializer
is an arrow function or a function expression, also when wrapped in one call
such as `forwardRef(...)` or `memo(...)`. A PascalCase constant that holds an
object is not a component.

## Input

`--primitives-spec <file>` (default `primitives-spec.json`), a JSON file relative to `--root`:

```json
{
  "designSystem": "@acme/ui",
  "componentsDir": "src/components",
  "indexFile": "src/components/primitives/index.tsx",
  "maxCompositionDepth": 4,
  "variantCategories": ["pill", "badge", "skeleton"],
  "primitives": [
    { "component": "StatusPill", "module": "primitives" },
    { "component": "PageHeader", "module": "primitives" },
    {
      "component": "TextInput",
      "module": "forms",
      "compositionExempt": "the design system has no unstyled native input"
    }
  ]
}
```

| Key                              | Meaning                                                                                                                                                                   |
| -------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `designSystem`                   | Package specifier, or prefix of one, that a component must reach. If it is absent, `primitives-not-composed` is not checked.                                              |
| `componentsDir`                  | Directory the `module` values are relative to. Default `src/components`.                                                                                                  |
| `indexFile`                      | Optional barrel file. Every name it exports, either declared there or re-exported with `export { X } from`, counts as allowed for `primitives-duplicate-local-variant`.   |
| `maxCompositionDepth`            | How many relative-import hops the composition search follows. Default `4`. Keep it bounded: without a limit, a barrel makes every file appear to reach the design system. |
| `variantCategories`              | Category words for the duplicate-variant rule. Default `[]` (rule off).                                                                                                   |
| `primitives[].component`         | Component name.                                                                                                                                                           |
| `primitives[].module`            | Directory under `componentsDir` that owns the component. If it is absent, `primitives-name-shadow` is not checked for that component.                                     |
| `primitives[].compositionExempt` | A reason string. If present, the component is not checked for composition. Keep the reason, so that every exemption stays deliberate and can be reviewed.                 |

Build the file by hand from your component catalogue. If the catalogue is a
Markdown table, a short script that maps each table row to one `primitives[]`
entry is enough.

Options: `--src <dir>` (default `src`) and `--primitives-spec <file>`. A missing
or malformed spec exits `2`. An empty `primitives` list, or a missing source
root, skips.

## Algorithm

1. List every `.ts`/`.tsx` file under `--src`, except `*.test.ts(x)`. Parse
   each file once with the TypeScript compiler API and cache the result. The
   composition walk below reuses the cache.
2. For each file, record two things:
   - its top-level exports: `export function X`, `export const X`,
     `export class X`, and the names in `export { X }`;
   - its component-shaped top-level declarations, whether or not they are
     exported.

   Build `exportsByName` and `declarationsByName`, each mapping a name to a
   list of `{ file, node }`.

3. For each listed component:
   - No export: `primitives-missing`, reported at the source root, line 1.
   - More than one export: `primitives-duplicate-export`, with every exporting
     file listed in the message.
   - Not exempt, and `designSystem` is set: run a breadth-first search over
     import specifiers, starting from the first exporting file. At each depth up
     to `maxCompositionDepth`, check every module specifier the current files
     import or re-export. If one equals `designSystem` or starts with
     `designSystem + "/"`, the component is composed. Otherwise, resolve each
     relative specifier (`./`, `../`) by trying the extensions `.tsx`, `.ts`,
     `.jsx`, `.js` and then `index.*`, and add the result to the next frontier.
     Track visited files. If the search ends without a match, report
     `primitives-not-composed` at the export.
   - `module` is set: report `primitives-name-shadow` for each declaration of
     the name whose file is not under `<componentsDir>/<module>/`.
4. Duplicate variants. The allowed names are every listed component plus every
   name `indexFile` exports. For each category, the offenders are the declared
   component names that contain the category word (case-insensitive) and are
   not allowed. If there are any, report one finding at the source root and
   list `Name (file)` for each offender, sorted. The finding is reported at
   line 1, column 1 (`src:1:1`).

## Example

`src/pages/orders-page.tsx` declares `function StatusPill()`, while
`primitives-spec.json` puts `StatusPill` in `primitives`. The check reports:

```
src/pages/orders-page.tsx:12:1: primitives-name-shadow: "StatusPill" is declared here, but the catalogue puts it in src/components/primitives/
```

`src/components/orders/order-badge.tsx` declares `OrderBadge`, and `badge` is a
category. `OrderBadge` is neither a listed component nor exported by the
barrel, so the check reports:

```
src:1:1: primitives-duplicate-local-variant: 1 local "badge"-shaped components exist outside the catalogue: OrderBadge (src/components/orders/order-badge.tsx)
```

## Test fixtures to write

- `positive/`: one listed component, exported once from its module, that
  imports `@acme/ui`. Expect no findings.
- `negative/`: one listed component that is missing; one exported from two
  files; one that never reaches `@acme/ui`; and two local `*Pill` components.
- `composition/`: a component that reaches the design system through an icon
  registry, two relative hops away. Expect no finding at depth 4 and a finding
  at depth 1.
- `declarations/`: a page that declares, without exporting, a component with a
  listed name. Expect `primitives-name-shadow`.
