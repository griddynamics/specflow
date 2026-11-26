---
type: Overview
title: "Mission Control V2 decisions"
description: "Product decisions D1 to D20 for the new Mission Control canvas and the public landing page, made before development"
status: stable
tags: [mission-control, decisions]
---
# Mission Control V2: decisions

Product decisions for the new Mission Control (the canvas prototype in this folder) and the public landing page,
made on 2026-11-24 before development. Each entry says what was decided and what it means for the build. The
prototype follows them; `README.md` describes the prototype itself.

## Integrations and connections

| # | Decision | What it means |
|---|---|---|
| D1 | An integration belongs to the project and runs on the account of the member who set it up | The card names that person ("Connected by Alex Kim"). This is how GitHub repositories (`added_by`) and the Atlassian and Slack connections work today, so the backend model stays |
| D2 | When that account loses access, everything that uses the integration stops | The person left the project, disconnected, or lost access in the app. Its nodes on the canvas stop and say why; another member reconnects. No silent fallback to someone else's account. V2 shows only the states the APIs report today (GitHub "reconnect required" or a failed clone, an expired Atlassian connection); no new stopped state (D18) |
| D3 | One organization per app per project | One GitHub organization (any number of its repositories), one Atlassian site, one Slack workspace |
| D4 | One integration, many uses | The integration is the connection only. Each use (a source, a gap channel, a plan destination) is added and edited on the canvas, with a little configuration of its own. The Integrations page only connects (for GitHub it also picks the repositories): no switches per use, no list of uses on the cards. Example for later: one Notion connection read as a source and written as a plan |
| D5 | Every object is its own node | Two repositories are two nodes, two Confluence links are two nodes, two Jira epics are two nodes. The same repository can be a source and, later, a plan destination |
| D6 | Anyone on the project can add sources, build the graph, build the plan and set up integrations | Only managing members stays with the owner (the Members dialog, as today) |

### Per app

| App | Integration (once per project) | On the canvas | On main |
|---|---|---|---|
| GitHub | Install the GitHub App, connect, pick the project's repositories (several, one organization) | Graph: pick repositories as sources, one node each. Plan: GitHub Projects as a task tracker, the plan's stories and tasks as issues | Connect and clone: built (#116). The build does not read the clones yet (D19). GitHub Projects: new |
| Atlassian | Connect the Atlassian site (per-user OAuth, already live) | Graph: a Confluence link or a Jira epic with all its issues, one node each. Gap: gaps posted as a Jira story whose sub-tasks are the problems to solve (one or several grouped). Plan: the plan in Jira, as today | Plan: built (#112, #114). Confluence and epic sources, gaps in Jira: new |
| Slack | Add the app to the workspace | Gap: the channel, connected with the command of main; **Test connection** posts a welcome message ("Welcome to the workspace of project Northwind ordering") so the person sees it arrive | Built (#115, #118); the test message is new |
| Telegram | Add the Graphico bot | Gap: a group, connected like a Slack channel, with the same test message | New |
| Notion | Connect the Notion workspace (per-user OAuth; pages are shared with the connection in Notion) | Graph: a page or database with its sub-pages or rows, one node per link. Plan: the stories and tasks as rows of a database made under a page, kept up to date | New: [../notion/INDEX.md](../notion/INDEX.md) |
| Linear | Connect the Linear workspace (OAuth) | Plan: a team and project; stories and tasks become issues, kept up to date like the Jira export | New |
| Upload, Google Drive, Obsidian | Not integrations: sources only, added per project with the plus | Graph: one node per upload, folder or vault. An Obsidian vault is uploaded from this computer as a folder or a zip of its Markdown notes, its [[links]] kept. No cloud option: Obsidian Sync is end-to-end encrypted and has no public API | Upload and Drive built; Obsidian new |
| MCP | Not an integration: always on | Added to the canvas by itself when the gaps are found and when the plan is built | Built |

Jira for now is read and written through the Atlassian app, not through MCP.

## Sources and the build

| # | Decision | What it means |
|---|---|---|
| D7 | This version imports a source once | A source is a snapshot at the time it is added; a changed document is added again. Live sync ("a living organism") is the next iteration |
| D8 | A rebuild keeps the gaps | Open gaps stay open, new information opens new gaps. The one exception: when new information settles a gap (it answers a contradiction or fills what was missing), Graphico closes it without asking. Not in V2: V2 keeps today's gap behaviour (D18) |
| D9 | The plan is never rebuilt from scratch | After a rebuild, or after gaps are answered, the plan becomes yellow again: **Replan**. A replan works like backlog refinement: it adds stories, changes them, or cancels the ones the new information makes obsolete. Claimed and finished work stays. Automatic replanning comes later. In V2 the **Replan** button runs today's replan, which makes the plan again; refining is later (D18) |

## Screens

| # | Decision | What it means |
|---|---|---|
| D10 | The canvas is the only screen | It is also the start page. No knowledge view, gap list, plan screen or MCP page: the old screens go. What needs more opens as a dialog on the canvas. Gaps are not answered in the app: people answer in Slack, Telegram or Jira, coding agents over MCP |
| D11 | Header: project menu, Members, Integrations | The project name in the header is a menu of projects with **Create new project** at the bottom. Members is a dialog (what Settings › General does today). Integrations is its own page. Settings goes |
| D12 | First run | A person with no project sees a "Create your first project" window over an empty canvas, as today |

## Brand and design

| # | Decision | What it means |
|---|---|---|
| D13 | The product is called Graphico, the stages Graph, Gap and Plan | No SpecGraph, SpecGap, SpecPlan. On the canvas the first zone is "Graph" and its decision is "Build graph" |
| D14 | The app uses the landing page's black and white style | `landing.css` is the visual source (Fira Sans and Fira Code, ink borders, offset shadows, GD yellow only for a decision). A design system of its own can be written from it later. GD Liquid Glass (`docs/design-system/`) is not used for V2, so the rule in `AGENTS.md` and `frontend/AGENTS.md` needs an exception for V2 |
| D15 | The real landing page drops the deep-dive pages | Reviews asked for changes and many integrations have shipped since; the deep dives come back only when rewritten |

## Build and deployment

| # | Decision | What it means |
|---|---|---|
| D16 | V2 is new routes inside the existing Next.js app | It reuses the data ports in `frontend/lib/`; the canvas and its dialogs are new components. The current screens stay until V2 replaces them |
| D17 | The landing page is a separate, public deployment; the app stays private | An npm workspace: `packages/ui` (shared presentational components and styles), `frontend/` (the app) and `site/` (the landing page as a static export in its own container) |
| D18 | V2 is the new UI plus the integrations | In: every connector in the per-app table (new ones included: Confluence links, Jira epics, Notion, Obsidian as sources; gaps in Jira and Telegram; GitHub Projects and Linear as plan destinations; the Slack test message). Connectors feed the processes that exist: a source becomes documents for today's build, a gap channel posts today's gaps, a plan destination exports today's plan like the Jira sync. No change to the graph build, the gap process or the plan process unless a connector cannot work without it. Out: plan refinement (D9), auto-closing settled gaps (D8), a uniform stopped state (D2) |
| D19 | GitHub repositories are cloned, not yet read by the build | V2 keeps the clone of #116 as preparation. The build does not use the snapshots: a clone does not fit the build as it is, and making it fit is a separate large task. Until then a repository node shows it is cloned; it adds nothing to the graph |
| D20 | V2 scope cut (2026-11-24): no new integration in V2; the canvas is at `/` | Notion, Linear, Telegram and Obsidian leave V2; each new integration comes as its own pull request. V2 keeps the connectors on the integrations that exist (Atlassian: Confluence links, Jira epics, gaps in Jira, the plan in Jira; GitHub: repositories, GitHub Projects; Slack: the channel and its test message). The canvas takes `/` and `/projects/<id>` now, so the later clean-up of the old screens only removes them. The prototype still shows the four as design history. Linear shipped in its own pull request (source, gap channel, plan destination): [Linear integration](../linear/INDEX.md) |

## Still open

Both questions were answered during the build as agent choices. The owner reviews them in the pull request.

- Which landing-page copy is the base for `site/`. **Agent choice AC3:** the `relaxed-bohr-ca4f18` copy (final polish and the Graphico rename), frozen into `landing-source/`. It is the newest and uses the same `landing.css` as the prototype.
- Who may remove an integration another member set up. **Agent choice AC6:** the member who connected it, or the owner. Removing stops everything that uses it (D2), so not every member.
