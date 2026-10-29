# Spec: `state-surfaces` (loading, empty and error states of data-backed surfaces)

Status: specification only. The kit does not ship this check as code. It needs
the names of the project's own state components, its query-hook layout and, for
one of its parts, the empty-state strings the product specification fixes.
Build it from this spec. Use the shared helpers in `checks/lib/` (`ts.mjs`,
`files.mjs`, `i18n.mjs`, `report.mjs`, `main.mjs`, `spec-input.mjs`) and the
usual CLI contract: exit `0` clean, `1` findings, `2` configuration or tool
error, and `skipped:` when there is nothing to check.

## What it enforces

| Rule                                           | Defect                                                                                                                                                                                                                                           |
| ---------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `state-surfaces-missing-rendered-state`        | A component that renders a list or table of query data does not render all three of: a layout-preserving loading skeleton, an empty state, and an inline error with a retry action.                                                              |
| `state-surfaces-hook-missing-error-handling`   | A hook named in `errorHandling.hookNamePatterns` calls a query or mutation hook but never references any of the configured error markers. For example, a hook that calls a slow upstream service must handle that service's "unavailable" error. |
| `state-surfaces-empty-state-outside-primitive` | A translation key whose base-locale value is one of the required empty-state strings is rendered into a JSX element other than the empty-state component.                                                                                        |
| `state-surfaces-empty-state-missing-action`    | A required empty-state string marked `needsAction` is rendered by an empty-state component that has no action prop.                                                                                                                              |

## Input

`--states-spec <file>` (default `states-spec.json`), relative to `--root`:

```json
{
  "queryHooksDir": "src/api",
  "queryHookCallees": ["useQuery"],
  "listContainerTags": ["DataTable", "table", "ul", "ol"],
  "listContainerRoles": ["grid", "list", "listbox", "table", "tree"],
  "skeletonTags": ["TableSkeleton", "CardListSkeleton"],
  "emptyStateTags": ["EmptyState"],
  "errorTags": ["InlineError"],
  "allInOneTags": ["ListStates"],
  "retryAttributes": ["onClick", "onRetry"],
  "retryCalleePattern": "^(?:refetch|retry|reload)",
  "errorHandling": {
    "hookNamePatterns": ["assistant", "recommend"],
    "markers": ["isServiceUnavailable", "SERVICE_UNAVAILABLE"]
  },
  "emptyStates": {
    "keySegmentPattern": "^(?:empty|none|nothing|no_)",
    "ignoreKeys": ["common.none"],
    "actionProps": ["action"],
    "strings": [
      { "value": "No orders yet.", "surface": "order list", "needsAction": true },
      { "value": "No order matches the current filters.", "surface": "order list" }
    ]
  }
}
```

Every key is optional. Leaving out `errorHandling` turns its rule off. Leaving
out `emptyStates.strings` turns the two empty-state rules off. Write the file by
hand. You can generate `emptyStates.strings` from the same source as the
`--required-strings` input of `i18n-catalogues`, so that both checks read one
list.

Options: `--src <dir>` (default `src`), `--locales <dir>` (default
`src/locales`), `--base-locale <code>` (default `en`) and `--states-spec <file>`.

## Algorithm

Parse every `.ts`/`.tsx` file under `--src` once with the TypeScript compiler
API, skipping `*.test.*` files.

1. **Query hooks.** In each `.ts` file under `queryHooksDir`, a query hook is
   an exported function, or an exported `const` arrow or function expression,
   whose body calls one of the `queryHookCallees`.
2. **Rendered list surfaces.** A `.tsx` file is a list surface when all three
   of these hold:
   - It renders a list container: a JSX tag in `listContainerTags`, or an
     element whose static `role` attribute is in `listContainerRoles`.
   - It binds the result of a query-hook call:
     `const q = useOrders()`, or the `data` property of a destructured result,
     `const { data: orders } = useOrders()`.
   - It renders that data: `<expr>.map(...)` inside a JSX expression, where
     `<expr>` is `q.data...` or the destructured `data` binding, possibly
     followed by property access.

   For each list surface, report `state-surfaces-missing-rendered-state` at
   the container element and list whichever of these are absent:
   - a tag from `skeletonTags`;
   - a tag from `emptyStateTags` (a tag from `allInOneTags` also counts);
   - a tag from `errorTags` (a tag from `allInOneTags` also counts), together
     with a retry: a JSX attribute in `retryAttributes` whose expression calls,
     or names, an identifier that matches `retryCalleePattern`.

3. **Error handling.** For each exported hook under `queryHooksDir` whose
   lower-cased name, with non-letters removed, contains a
   `hookNamePatterns` entry, and whose body calls a query or mutation hook:
   look for a reference to one of the `markers`, either as an identifier, as a
   property name, or as a static string. Search the hook body first, then the
   whole file, because a shared handler in the same module counts. If there is
   none, report `state-surfaces-hook-missing-error-handling` at the hook.
4. **Empty states.**
   1. Read the base-locale catalogue (see `checks/lib/i18n.mjs`).
   2. A key is an empty-state key when one of its dotted segments matches
      `keySegmentPattern` and the key is not in `ignoreKeys`.
   3. Map each empty-state key whose value, trimmed, equals one of
      `emptyStates.strings[].value` to that entry.
   4. For each `t('<key>')` call in a `.tsx` file whose static key is in that
      map, find the JSX element the call renders into. That is either the
      element owning the attribute the call is passed to, or the element
      whose child expression contains the call. If the call does not reach JSX
      in this file, skip it; never guess.
   5. If that element is not in `emptyStateTags`, report
      `state-surfaces-empty-state-outside-primitive`. Make one exception when
      the call is passed as a prop and the element's own module renders an
      empty-state tag. To check this, follow one relative import of the tag
      and, if that module is a barrel, one `export { Tag } from` re-export.
   6. If the element is an empty-state tag, the entry has `needsAction`, and
      none of the `actionProps` is present, report
      `state-surfaces-empty-state-missing-action`.

## Example

`src/components/orders-table.tsx` calls `useOrders()`, renders `<table>` and
maps `orders.data.items` inside JSX. It renders `<TableSkeleton/>` and
`<EmptyState/>`, but its error branch is a plain `<p>`. The check reports:

```
src/components/orders-table.tsx:18:7: state-surfaces-missing-rendered-state: query-backed list/table does not render: retryable inline error
```

## Test fixtures to write

- `positive/`: a table surface that renders all three states. Add a surface
  that uses an all-in-one `ListStates`. Add a form and a provider that call the
  hook without rendering a list; neither is judged.
- `negative/`: a list surface without a skeleton, an empty state or a retry.
- `bare-skeleton/`: a list surface that renders a generic `Skeleton` instead of
  a layout-preserving one.
- `empty-states/`: a required string rendered by `<p>`, by a wrapper that does
  render `EmptyState` (accepted), through a barrel (accepted), and by an
  `EmptyState` with no action when one is required.
