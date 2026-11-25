---
type: Design
title: "Mission Control V2 screens as built"
description: "The V2 screens as they are built in frontend/app/(v2): routes, header, first run, Integrations, the canvas with its hubs, nodes, dialogs and connectors, the states and their words, and where the build differs from the prototype and why"
status: stable
tags: [mission-control, ui, v2]
---
# Mission Control V2: the screens as built

The prototype in this folder ([README.md](README.md)) stays the design source for look and flow; this page says what was built, with the
copy as it is in the code (`frontend/components/v2/`), and where it differs from the prototype and why. Decisions: [DECISIONS.md](DECISIONS.md)
(D1-D20); agent choices (AC) are in `plans/MISSION-CONTROL-V2/HANDOFF.md`. Style: the Graphico style of `packages/ui`
(root class `.gx`, source `landing.css`), not the GD design system (D14). Server calls: [integrations-api.md](integrations-api.md).

## Routes

| Route | Screen |
|---|---|
| `/` | The newest project's canvas (API list order, AC37); a person with no project sees the first-run window; `?slack=` shows the Slack return toast |
| `/projects/<id>` | The canvas of the project; `?add=<kind>` opens that connector's form and cleans the URL (AC43) |
| `/projects/<id>/integrations` | Integrations; `?setup=<app>&next=<path>` opens the app's set-up and returns to `next` (AC8, AC38) |

A project the person is not a member of, or one that does not exist, shows "This project does not exist, or you are not a member of it."
inside the shell. The V1 screens (`/projects/<id>/mission-control|gaps|plan|specbot|settings`, `/connections`, `/guides`) still work until the
clean-up PR deletes them (AC1, D16).

## Header and first run

Brand, the project menu (**Projects**, with "+ Create new project" under the list), **Mission Control**, **Members** (a dialog), **Integrations**,
the theme switch and the avatar menu (**Sign out**) (AC36). With no project only the brand, the theme switch and the avatar show.
The first-run window ("Welcome to Graphico", "Create your first project") cannot be dismissed; the same dialog opened from the menu can.
Members is the Settings General tab in a dialog: the owner renames the project, adds people by email (known people are added, other emails
become invites), removes members and cancels invites; a collaborator sees the list read-only and can leave.

## Integrations

One card per app (Atlassian: "Jira and Confluence", GitHub: "Repositories", Slack: "Channels"), then **MCP** ("Coding agents", action **Connect**), then
the built-in list (Upload and Google Drive, always on). MCP is a card of the page only: the project has no connection to it, so it is not an
`IntegrationApp` of the API and has no row; its dialog (`?setup=mcp` works like the other cards, with `next`) holds the three connect steps with
their copy boxes and the note about other clients, and Done.
A card says who connected it. Health words are API facts only (AC20, AC39): **Active**, **Reconnect required**, **Stopped**; an app that is off
on the server is a disabled "Not on this server" card. **Set up** opens one dialog per app: Atlassian (consent, then the site), GitHub
(consent, install the App, pick repositories), Slack (invite the app, run `/specflow connect <project id>`; AC14 keeps the command name).
Every step a person does outside Graphico has a **Show me how** control: a closed disclosure that opens the steps inline (Slack, Atlassian, GitHub,
Google Drive, from `docs/slack`, `docs/github`, `docs/jira-sync`, `docs/sources`), or, on the canvas MCP dialog, a link to the MCP dialog here.
A code box (`CopyRow`) has a copy icon inside it on the right; a check shows for about 2 s after a copy.
**Manage** disconnects (the member who connected it, or the owner, AC6); what uses it stops (D2).

## The canvas

Three stages, "01 · Graph", "02 · Gap" and "03 · Plan", split by thin lines, each with a hub, its nodes and a plus tile. The view model is
pure (`canvas-model.ts`) over the ports; there is no endpoint of its own (AC9).

**Hubs.** They carry no name or count under them; the status is in the tooltip. Only a decision shows under a hub (AC41, alternative A):
**Build graph** or **Update graph** under Graph, **Build plan** or **Replan** under Plan. Yellow marks an open decision and nothing else; if both
are open the graph keeps the yellow. Graph: waits for a source, ready to build, building (a ring fills, the step shows). Gap: waits for the graph;
"Waits for the gap run" while the gap run was never made; "Looking for gaps" while it runs; then "N found · M answered" with a solid
"N critical" pill. Plan: waits while gaps are found, ready to plan, planning, then "N stories · M tasks". **Update graph** is due when a source was
added or files removed after the last build started, or the last build failed; **Replan** when a graph build finished after the screen first saw
the plan (session-local, AC40). Pressing the Gap hub opens a read-only list of open gaps, blockers first, 50 lines (AC47): gaps are answered in
Slack, Jira or over MCP, never here (D10).

**Nodes.** One node per object (D5) with the app's mark, a name, one line of meta and a state: `on` (ink), `busy` (spinner, moving wire),
`wait` (grey, dashed wire), `stopped` (badge). A node stops only for what an API reports (D2). The wire between a node and its hub moves only
while data moves. The background graph is decorative and grows with build progress (AC10). MCP joins by itself, marked "Automatic".
Pressing a node opens its dialog: app, who set it up, state in a sentence, and for a source (upload, Drive folder, Confluence link, Jira epic) the
files inside it (first page of 200, **Show more** for the next; name, kind, and a state with the V1 reason words only when the file is not done),
for an upload batch **Rename** (a field with Save and Cancel; a refused name, a source that is not an upload or a source that is gone are said in
words), **Remove** (sources and repositories only, owners, AC46) or
**Reconnect** (a stopped node, to the Integrations page of that app).

**Plus.** The plus of a stage lists its own connectors first and the others dashed under "Connections for the other stages". A connector that needs
an app that is not set up says "Set up first" and goes to the Integrations set-up and back with its form open. Every form with a check runs it
before adding, and a changed input is a new check.

### Connectors

| Kind | Stage | Needs | Form (title) and check | Node |
|---|---|---|---|---|
| `upload` | Graph | | Add files; an optional name above the drop zone (placeholder: the default node name) | The name the person gave, else `Upload 10:42` / `Upload 25 Nov` (AC48), file counts, imports |
| `gdrive` | Graph | | Connect a Google Drive folder; Test connection, Add source | folder name, files done of total |
| `github` | Graph | GitHub | Add repositories; picks from the account's readable repositories (AC45) | repository, clone state (not read by the build, D19) |
| `confluence` | Graph | Atlassian | Add a Confluence link; Check link, Add source (AC50) | page or space, "N pages" while it imports |
| `jira_epic` | Graph | Atlassian | Add a Jira epic; Check epic, Add source | the epic key, "N issues" (AC51) |
| `slack` | Gap | none (AC44) | Connect a Slack channel; invite, connect command, Test connection, Add channel (AC49) | `#channel`, "N found · M answered" |
| `jira_gap` | Gap | Atlassian | Send gaps to Jira; space key, grouping, Check access, Add channel | `Jira · <space> gaps`, "N posted · M answered" (AC52, AC53) |
| `jira` | Plan | Atlassian | Send the plan to Jira; space and epic keys, Check access, Add destination | `Jira · <epic>`, syncing, synced |
| `github_plan` | Plan | GitHub | Send the plan to GitHub Projects; repository, project picked or created, Check access, Add destination (AC61) | `GitHub · <project>`, "N issues · GitHub" |
| `mcp` | none | | Coding agents over MCP (not listed in plus): the stage's notice, Show me how (a link to the Integrations MCP dialog), then the stage's tools, each with one sentence of what it does | "Automatic" |

Every check says what to fix in words from the API's closed codes (another site, not found, not allowed, not connected, needs a reconnect);
`github_permissions_missing` says to approve the Issues and Organization projects permissions of the GitHub App for the installation.
Stopped nodes: a lost Atlassian or GitHub connection (reconnect), or the closed code the last pass ended on.

## Differences from the prototype

| Prototype | Built | Why |
|---|---|---|
| Notion, Linear, Telegram, Obsidian | Not in V2 | D20: each comes as its own pull request |
| Integration state in localStorage | Project bindings from the API | D1, AC4 |
| Slack `/graphico connect` | `/specflow connect <project id>` | Main wins (AC14) |
| A Jira plus asks for space and epic keys only | Also Check access as the bound member | Same rules as the Jira tab on main |
| Decision buttons on a seam or a dock | On the hub (alternative A) | AC41 |
| Gaps answered in the prototype's list | Read-only list | D10 |
| Slack added after a posted test only | A channel whose test failed is already a node; the form closes and says what to fix | The binding exists once the command ran (AC49) |
| "Set up" sets a switch | OAuth consent per app, then the canvas form | Integration flows of main (AC8) |
| Remove on every node | Sources and repositories only; the owner | AC46 |
| Header: project menu, Members, Integrations | Also a **Mission Control** link and a theme switch | The brand link goes to the newest project, so from a project's Integrations page the person needs a way back to that project's canvas; the style has a light and a dark theme and V1 had a switch (AC36) |
| A hub's status shows only as a tooltip | The same text is in the page for screen readers, hidden visually | The hubs carry no visible name or count (D14); a tooltip is not reachable by keyboard or by a screen reader |
| A failed read leaves the canvas as it was | A notice with "Try again" over the canvas when the project's status cannot be read | An empty canvas that is really a failed read misleads |
| Build starts at once | The dialog waits for the API's answer and says in words when a start is refused (a build already running, the service down) | The hub is marked as starting only when the start was accepted |
