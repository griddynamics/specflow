<!-- BEGIN:nextjs-agent-rules -->

# This is NOT the Next.js you know

This version has breaking changes — APIs, conventions, and file structure may all differ from your training data. Read the relevant guide in `node_modules/next/dist/docs/` (resolved from this file's directory; in monorepos the `next` package may not be visible from the repo root) before writing any code. Heed deprecation notices.

This block is written and re-added by `next dev` — verify at `node_modules/next/dist/server/lib/generate-agent-files.js`. Removing it from a diff only re-creates the uncommitted change; committing it with your work keeps the tree clean.

<!-- END:nextjs-agent-rules -->

# Screen header rule

Every screen puts its actions in the V2 header row (`components/v2/shell/V2Header.tsx`, the "App" navigation):
the brand, the theme switch, the account, the project menu and the Members and Integrations links live there,
on the same horizontal line. Do not put a page title (`<h1>`) or a tab row at the top of the screen body. The
body starts with the screen's content; a dialog's title lives in the dialog. Mockups follow the same rule.

Every screen is Graphico: the UI kit of `packages/ui`, its source `docs/mission-control/landing.css`
(decision D14 in `docs/mission-control/DECISIONS.md`).
