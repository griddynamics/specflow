<!-- BEGIN:nextjs-agent-rules -->

# This is NOT the Next.js you know

This version has breaking changes — APIs, conventions, and file structure may all differ from your training data. Read the relevant guide in `node_modules/next/dist/docs/` (resolved from this file's directory; in monorepos the `next` package may not be visible from the repo root) before writing any code. Heed deprecation notices.

This block is written and re-added by `next dev` — verify at `node_modules/next/dist/server/lib/generate-agent-files.js`. Removing it from a diff only re-creates the uncommitted change; committing it with your work keeps the tree clean.

<!-- END:nextjs-agent-rules -->

# Screen header rule

Every screen puts its heading in the shell's header row, on the same horizontal line as the project picker:
its tab bar, view switch or breadcrumb goes into `<ShellHeader tabs=... tools=...>`
(`components/shell/ShellHeader.tsx`), as Plan, Decisions, Connections and Guides do. Do not put a page title
(`<h1>`) or a tab row at the top of the screen body. The body starts with the screen's content. Mockups follow
the same rule.
