---
type: Design
title: "Mission Control prototype"
description: "The clickable design prototype of the Mission Control canvas: how to open it, its screens and what it leaves out"
status: stable
tags: [mission-control, prototype]
---
# Mission Control prototype

A clickable design prototype for the next Mission Control. People connect sources and press one button;
everything else runs on its own and shows on one canvas. It is a design source for the frontend,
not product code. All data on the page is invented. The product decisions behind it are in
[DECISIONS.md](DECISIONS.md).

Open it with any static server, for example:

```bash
python3 -m http.server 8766 --directory docs/mission-control
```

## The canvas

Three zones, split by thin lines. Every zone has a hub (Graphico's own engine for the stage), the
connections around it and a plus button.

| Zone | Hub | Connections |
|---|---|---|
| 01 Graph: what Graphico reads | Graph (graph or brain icon) | Upload, Google Drive, GitHub repositories, Confluence links, Jira epics, Notion pages, Obsidian vaults |
| 02 Gap: where people answer | Gaps | A Slack channel, a Telegram group, Jira (a story with sub-tasks) |
| 03 Plan: where the work goes | Plan | Jira, GitHub Projects, Linear |

An Obsidian vault is uploaded from this computer, as a folder or a zip.

Every connection opens its own set-up form with a check before it is added (a link, an epic key, a
Jira space, a repository, a Linear team). DECISIONS.md says which are built on main and which are
new in V2.

Every object is its own node: two repositories are two nodes, two Confluence links two nodes. A
node shows the object's name; its icon shows the app.

The hubs carry no name or count under them: the zone labels name them, and the status is in the
hub's tooltip. Only a decision ("Build graph", "Build plan", "Replan") shows under a hub.

MCP joins the canvas by itself, as a connection marked "Automatic": in Gap when the gaps are found,
and in Plan when the plan is built. Selecting it shows how a coding agent connects and which tools
that stage has.

The plus button lists the connections for its own stage first, and
those for the other stages below them, so people learn what fits where.

## Header

The canvas is the only screen. The project name in the header opens a menu of projects with
**Create new project** under it. A person with no project gets a "Create your first project"
window that cannot be closed (the prototype's "First run" button shows it).

There is no Settings screen. **Members** in the header opens a dialog with the project's people and name
(docs/projects/ui.md): the owner renames the project, adds people
by email with suggestions (a known person is added, any other email becomes an invite), removes
members and cancels invites, each after a confirmation. A collaborator sees the list read-only and
can leave the project. The dialog's "view as" switch is for the prototype only.

## Integrations (`integrations.html`)

**Integrations** in the header opens its own page. It only connects: one card per app, three in a
row, with the app's name, what it holds and who connected it. No page title row, and no list of
uses: what each app is used for shows on the canvas. An integration works with the account of the
member who set it up; when that account loses access, what uses it stops. What each app can do:

| Integration | 01 Graph | 02 Gap | 03 Plan |
|---|---|---|---|
| Atlassian (Jira and Confluence) | Confluence links, following their links; Jira epics with their issues | Gaps as a Jira story with sub-tasks | The plan in Jira |
| GitHub | Repositories (cloned; the build does not read them yet) | | GitHub Projects |
| Slack | | Gaps in a channel | |
| Telegram | | Gaps in a group | |
| Notion | Pages and databases | | |
| Linear | | | The plan in a team's project |

A card is grey and dashed until someone sets it up; then it is ink with a shadow. Set up is one
step: connect (sign in to the app); for GitHub, also pick the project's repositories (one
organization). There are no switches: a use is on when it is on the canvas, and it is edited there
(which repository, which channel, which Jira epic). Upload and Google Drive are not integrations:
they are only sources, added with the plus on the canvas. They are listed under the cards with MCP.
There is no web app for answering gaps: people answer in Slack (or Jira, later), agents over MCP.

On the canvas, a plus card for an app that is not set up says "Set up first". Selecting it opens
that app's set-up on the Integrations page; after "Turn on", the browser goes back to Mission
Control with the app's form open, to finish adding the connection. The prototype keeps the set-up
state in the browser (localStorage).

On the canvas: a GitHub plus lists the project's repositories not on the canvas yet, and each one
picked becomes a node. A Slack plus says to invite the app to the channel and run the connect
command there; **Test connection** posts a welcome message, then the channel is added (one channel
per project). A Jira plus asks for the space and epic keys and checks access first.

## Colour

| Colour | Meaning |
|---|---|
| Grey, dashed | Not active yet: not connected, or the stage before it is not built |
| Grey, solid | Connected, but it waits for the stage before it |
| Ink (black in light mode, white in dark mode) | Connected and working |
| Moving dashes on a wire | Data is moving: an import, a build, questions going out, a sync |
| GD yellow `#FFC002` | A decision a person makes now. There are two: build the graph, and build (or replan) the plan |

Yellow is never a status. Critical gaps are a solid ink count on the Gaps hub. The plan becomes
yellow as soon as the gaps are found, answered or not: open critical gaps hold only the work they
block. After the graph is rebuilt or gaps are answered, the plan is yellow again as **Replan**: the
plan is refined (stories added, changed or cancelled), never made again. If both decisions are open
at once, the graph build keeps the yellow and the plan button is ink.

One line runs across the canvas: sources, Graph, Gaps, Plan, destinations. Gaps sits in the
middle of its zone; its channels (Slack, Telegram, Jira) and their plus button are a row right under it.

## The flow (the scenes in the prototype bar)

1. Nothing connected: three plus buttons of the same size, and everything else grey.
2. Importing: each source has a progress bar and a spinner badge; the wires to the hub move.
3. Ready to build: decision 1. The build button is yellow.
4. Building: a ring fills around the Graph hub, the hub shows the step (one of the 11 build
   steps), and the background graph grows from the hub outward.
5. Gaps found, ready to plan: the Gaps hub turns ink, with a "3 critical" count, and questions flow
   to Slack. When they are all sent, decision 2: the plan button is yellow.
6. Plan in Jira: the plan is built and syncs to Jira. The wire moves only during a sync.
7. New information, replan: the plan is yellow again with "Replan".
8. A connection stopped: the GitHub account behind the integration lost access, so both repository
   nodes stop with a "!" badge; selecting one offers to reconnect GitHub.

A new source added after a build makes the button "Update graph". Sources are read once, when added.

## Alternatives to decide

| Question | Options in the prototype | Built (agent choice, owner reviews) |
|---|---|---|
| Where are the two decision buttons? | A: the hub itself (Graph, then Plan) turns yellow. B: a round gate on the line before the stage it starts (Graph\|Gap, then Gap\|Plan). C: a dock at the bottom of the canvas, with the reasoning level inline | A: the hub carries the decision (AC41); the reasoning level is chosen in the Build dialog |
| Graph icon | A graph or a brain | A small graph glyph (a hub with satellites) of `packages/ui` |
| Background | A graph that grows with the knowledge, a dot grid, or plain | The growing graph, decorative (AC10) |

The screens as built, their words and the differences from this prototype: [ui.md](ui.md).

## Style

The page reuses `landing.css` from the landing page (black and white, Fira Sans and Fira Code,
1.5 px ink borders, offset shadows, light and dark). V2 uses this style (DECISIONS.md D14); the GD
design system the V1 screens used is retired with them.
Vendor marks are the `simple-icons` paths the app already uses, drawn in one colour. Slack has no
mark there, so its tile shows a channel sign.
