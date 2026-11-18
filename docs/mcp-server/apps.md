---
type: Design
title: "MCP Apps interface for gaps"
description: "Research and draft design: engineers answer gaps in an MCP Apps view inside their coding agent, with a text path for hosts without views"
status: draft
tags: [mcp]
---
# apps.md: research and draft design of an MCP Apps interface for gaps

Engineers answer the gaps of the gap module ([../graph/gap/answers.md](../graph/gap/answers.md)) from their own coding
agent (Claude first). The agent shows an interactive view instead of only text from read and write tools. This
document holds the research (state of November 2026), the decisions taken so far and the open questions. It is a
draft: no tool, route or rule in it is built.

## Summary

1. MCP Apps is the first official MCP extension: `io.modelcontextprotocol/ui`, stable specification 2026-01-26. A tool
   names a `ui://` HTML resource. The host shows the resource in a sandboxed iframe. The view calls our tools
   through the host, with the user's connector session.
2. The Claude Code CLI in a terminal does not show MCP Apps views. It drops the view and gives the model only the
   text result (issue `anthropics/claude-code#95149`, open, no answer from a maintainer; no changelog entry up to
   2.1.326). Claude Desktop chat and claude.ai show views. For the Claude Desktop Code tab the reports do not agree;
   the spike checks it.
3. Our server can do this at low cost. The `mcp` package we use (2.3.0) has the extension built in:
   `mcp.server.apps.Apps`, with `@apps.tool(resource_uri=..., visibility=[...])` and
   `add_html_resource(..., csp=..., domain=...)`.
4. The design: the agent finds, the person decides. One view, `ui://specflow/gaps`, has two screens, Browse and
   Answer. The model can open the view and suggest an answer. Only a click in the view sends an answer, through
   tools that the model cannot call. Every tool also returns the full text and a link to the web gap card, so the
   CLI and other hosts still work.

## Decisions

| # | Decision |
|---|---|
| D1 | Target: the view in the Claude Desktop Code tab, if the spike shows that it renders there. The Claude Code CLI gets the form of D6, and the text with a link to the web gap card where no form can show |
| D2 | Assignment of gaps (unassigned, mine) is out of scope for now. The browser lists the open gaps with filters |
| D3 | Upload and attachments are out of scope for now |
| D4 | An answer comes only from a click in the view, through app-only tools. The model may suggest an answer and never sends one. Dismissing stays in the web app |
| D5 | Next step: the spike ("Delivery"). If the Code tab does not render views, the next target is chosen with the spike's evidence |
| D6 | In a terminal, a person answers a gap in an elicitation form that the server asks for ("Terminal dialogs"). The answer goes from the client to the server, never through the model. Claude's own question wizard is not an answer path |

## MCP Apps in short

| Part | What it is |
|---|---|
| Binding | The tool has `_meta.ui.resourceUri` set to a `ui://` URI. The resource has the MIME type `text/html;profile=mcp-app` and is one self-contained HTML file |
| Visibility | `_meta.ui.visibility` is `["model"]`, `["app"]` or both (the default). The model does not get an app-only tool. Claude Code does not give app-only tools to Claude |
| View to host | JSON-RPC over `postMessage`. `tools/call` and `resources/read` go through the host to our server. `ui/message` adds a user message (the user must agree; at most 8,000 characters). `ui/update-model-context` replaces the context that the view gave the model. Also `ui/open-link` and `ui/request-display-mode` (inline, fullscreen, pip) |
| Host to view | `ui/notifications/tool-input`, `tool-result`, `tool-cancelled`, `host-context-changed` (theme, size), `ui/resource-teardown` |
| Sandbox | The default content security policy is `default-src 'none'` and `connect-src 'none'`. The view reaches only the origins in `_meta.ui.csp.connectDomains`; all other data goes through `tools/call` |
| State | The view loads again from its HTML each time it opens. State stays on the server |
| Claude | `ui.domain` is the first 32 hex characters of the SHA-256 of the server URL, then `.claudemcpcontent.com`. Calls of a remote connector come from Anthropic's backend, not from the user's computer. A tool result of more than about 150,000 characters breaks in the sandbox. The side panel and full screen are available to some organizations only |
| Files | There is no upload API: only `app.downloadFile`. File uploads are a draft of a working group. It is not known whether `<input type=file>` works in Claude's sandbox |

## Where a view renders

| Host | View | Elicitation form | For an engineer with a coding agent |
|---|---|---|---|
| Claude Code CLI (terminal) | No, text only | Yes, interactive sessions only | Text path only |
| Claude Desktop, Code tab | Not known: the spike checks it | Not known | Best fit if it renders: the repository and the view in one place |
| Claude Desktop, chat | Yes, local and remote servers | Not known | A view, but no repository |
| claude.ai | Yes, remote connector | Not known | As Desktop chat |
| VS Code with GitHub Copilot | Partly (no fullscreen, no pip) | Not known | Possible second client |

## What exists today

| Area | Fact |
|---|---|
| Answer contract | `answer_gap(AnswerInput)`: exactly one of `option_rank` (2 to 4 options, one Recommended, preselected for `major`) and `own_text` (1 to 4,000 characters); optional `edits` from `preview_edits`; `placement` `amend` or `document`; `updated_at` is required (`409 gap_stale`) ([../graph/gap/answers.md](../graph/gap/answers.md)) |
| Card | Chips, title, question, why, `ABOUT` and `IMPACTS` nodes, quoted lines with document, section and lines, options with an edits preview, Other, placement ([../graph/gap/answers.md](../graph/gap/answers.md) "The question card") |
| MCP gap tools | `list_gaps` reads, `add_finding` raises a development gap. No tool answers a gap. AC12: only people resolve or dismiss gaps, in the web app ([tools.md](tools.md), [access-control.md](access-control.md)) |
| MCP server | `MCPServer` of `mcp` 2.3.0, stateless HTTP with JSON responses (no event stream), a 1 MiB request limit, a thin proxy to the API's agent routes with the caller's token, one tool registry, no resources ([architecture.md](architecture.md)) |
| Identity | OAuth through the organization's identity provider. The user is the token's subject. No tool takes a user argument (AC9) |
| OAuth client | One public client, `specflow-mcp`, with loopback redirects on port 8765 only ([auth.md](auth.md)) |
| Assignment | None. A gap has `created_by` and, for a development gap, `story_id`. Stories have claims with one holder ([../graph/gap/data-model.md](../graph/gap/data-model.md)) |
| Members | A project has one owner and collaborators ([../projects/overview.md](../projects/overview.md)); any member may answer a gap |
| Web app | The Gaps screen already does the full answer flow with the GD Liquid Glass design system ([../design-system/USAGE.md](../design-system/USAGE.md)) |
| Deferred items | [later.md](later.md) already lists "MCP Apps tables" and "Streamed responses" (elicitation) |

## Interaction model

```
Engineer: "Show the open gaps of this project"
  -> the model calls browse_gaps(project_id)                 [model and app, opens the view]
  -> the host shows ui://specflow/gaps, Browse screen
Engineer opens a gap                                          -> get_gap            [app only]
  Answer screen: question, why, quotes, A to D, Other, placement
Engineer clicks "Ask Claude"  -> ui/message: "Look at GAP::7C2A91E04B3D in this repository and suggest an answer"
  -> the model reads the code and calls suggest_gap_answer   [model, opens the view]
  -> the view shows the suggestion as a preselected draft; nothing is sent
Engineer clicks "Submit"                                      -> submit_gap_answer  [app only]
  -> ui/update-model-context: "GAP::7C2A91E04B3D answered with option B" (data, not instructions)
```

The model never sends an answer; a person does, with a click. This keeps the intent of AC12 (people decide what
goes into the spec) and adds what the coding agent knows: the repository.

## Tools

One resource, `ui://specflow/gaps` (one bundle, one content security policy), has two screens. Every tool returns
structured content and one text block with the same JSON, as the tools do today. Each gap row and card has `web_url`,
the web gap card (`/projects/{pid}/gaps?gap=<id>`).

| Tool | Visibility | Screen | Agent route | Purpose |
|---|---|---|---|---|
| `browse_gaps` | model, app | Browse | `GET /agent/gaps`, extended | Open gaps of route `ask`; filters: status, severity, kind, origin, story; paging |
| `open_gap` | model, app | Answer | `GET /agent/gaps/{gap_id}` | Opens the card of one gap, for example from a brief or `list_gaps` |
| `get_gap` | app | none | `GET /agent/gaps/{gap_id}` | The card data with `updated_at` |
| `preview_gap_edits` | app | none | `POST /agent/gaps/{gap_id}/edits_preview` | Own text to edits (a diff) |
| `submit_gap_answer` | app | none | `POST /agent/gaps/{gap_id}/answer` | `option_rank` or `own_text`, `edits`, `placement`, `updated_at` |
| `suggest_gap_answer` | model | Answer | none | The model's draft, shown preselected; it writes nothing |
| `answer_gap` | model | none | `GET /agent/gaps/{gap_id}`, `POST .../answer` | Takes only the gap id and asks the person in a dialog ("Terminal dialogs", D6) |

`list_gaps` stays as it is for agents that build a story. Not planned: a dismiss tool (D4), assignment tools (D2),
an upload tool (D3).

## Inputs

| Gap element | Widget | Rule |
|---|---|---|
| Options (2 to 4) | Single-choice cards labelled A to D, each with label, detail and an edits preview that opens on click; one has the badge Recommended | Preselected only for `major`; never more than one recommendation |
| Other | A text area with a counter (1 to 4,000), then "Preview changes" (`preview_gap_edits`), then the diff | When the preview fails, offer placement `document` with the text as written |
| Placement | A switch: "Amend the source section" or "Clarifications document" | Preset from the option's edits; a refused switch shows the reason (`contradiction_amends`, `no_anchor`) |
| Kind `contradiction` | The two quoted lines side by side above the options | Display only; the answer contract does not change |
| Development gap under investigation | "Options are being prepared"; Other works at once | As on the web card |
| Reconcile follow-up | The options as buttons ("Keep the upload", "Drop it") | Options without edits are valid only here |
| Changed gap | A banner with "Reload" on `gap_stale` | `updated_at` from `get_gap` |
| Not a gap | A link to the web card | D4 |

Severity and status show as text and an icon, never by color only. Gap text is untrusted document content: the
view sets it with `textContent`, never `innerHTML`.

## Terminal dialogs

Where no view renders (the Claude Code CLI), the person answers in a form that Claude Code shows on the server's
request (D6). The model calls `answer_gap` with the gap id only; it never sees the form and cannot write the answer.
People never need to know the word "form": they ask in their own words ("which spec questions are open?", "let me
answer the gaps"), and the server instructions and the tool descriptions lead Claude to `answer_gap`; the prompt
`answer_gaps` is the slash command for the same.

| Step | What happens |
|---|---|
| 1 | The model calls `answer_gap(project_id, gap_id)` |
| 2 | On protocol 2026-07-28 the server answers with `input_required`: one form-mode elicitation request and a sealed `requestState`. No session and no event stream are needed |
| 3 | Claude Code shows the form: a dialog in the terminal, a card in the Desktop Code tab and at claude.ai/code |
| 4 | Claude Code calls `answer_gap` again with the person's answers and the `requestState` |
| 5 | The server checks the answer as `submit_gap_answer` does (`updated_at` included) and records it as the token's user, with `via` `elicitation` |

The form is flat (the specification allows only primitive fields). It never needs the right arrow key: every option
is on screen.

| Part | Content |
|---|---|
| Message, first three lines | The question; a line that names every option (`A) 14 days · B) 30 days (recommended) · ... · or your own answer`); why it matters |
| Message, folded | Each option with its detail, the quoted lines with their source, and last the gap id, severity, kind and title |
| One yes/no field per option | Titled with the option's short label, `(recommended)` on one; the recommended one is ticked for `major` |
| `own_text` | "Or write your own answer (untick the options above)": 1 to 4,000 characters |

Exactly one ticked option, or no ticked option and an own answer, is an answer; anything else returns a message that
says what to change. There is no placement field: an option keeps the placement of its edits, and an own answer takes
a second round in which the server previews the edits (`preview_edits`) and asks to confirm them, the diff in the
message; when the preview fails, the text goes to the clarifications document as written. Declining or cancelling
records nothing. A single titled choice (`select`) was the first layout; its options open only with the right arrow
key, which the first person who tried it did not find. The demo keeps it for comparison (Q10).

Protocol and transport, as the demo shows with the SDK's client:

| Client protocol | Stateless HTTP (the `mcp` role) | stdio |
|---|---|---|
| 2026-07-28 | Works: the `input_required` round trip | Works: the same round trip |
| 2025-11-25 and older | No form: a request during the call needs a session. The tool answers with an error that names the protocol, and `web_url` | Works: `elicitation/create` during the call |

Claude Code's v2 MCP runtime speaks 2026-07-28 with HTTP servers that support it (from Claude Code 2.1.232 where it
fetches feature flags, from 2.1.274 otherwise). A non-interactive session (`claude -p`) cancels a form; the tool then
returns the text with `web_url`. The SDK seals `requestState` with a per-process key by default; several `mcp`
replicas need one shared key (a new setting).

Verified with the demo in Claude Code 2.1.327 (the CLI): the `input_required` result of an ordinary tool call opens
the dialog "MCP server ... requests your input" ("Fulfilling input required by 'tools/call' (round 1)"), with
the titled choice preset to the recommended option, the text field and the placement; Accept and Decline reach the
server. The dialog shows the first lines of the message and folds the rest ("+N more lines", opened with the right
arrow key); it shows only the chosen value of a titled choice (the list opens with the right arrow key) and cuts a long
option title to the terminal's width. Hence the layout above: the question and every option in the first lines, one
line per option, short titles.

Claude's question wizard (its `AskUserQuestion` tool) was weighed and is not an answer path:

| | Elicitation form (D6) | Claude's question wizard |
|---|---|---|
| Who opens it | The server, on every call of `answer_gap` | The model, when instructions tell it to; not guaranteed |
| Path of the answer | Client to server; the model never sees it | Person to model to a model-visible answer tool |
| Layout | Message and flat fields; no diff | Up to 4 questions as tabs, 2 to 4 options, "Other", a preview pane per option |
| D4 | Holds | Needs a guard (an approval prompt or a hook that compares the call with the person's choice) |

The wizard may later help a person pick which gaps to work on; no answer passes through it.

### Demo

`backend/examples/mcp_gap_dialogs_demo.py` serves one sample gap, with no sign-in and no storage: `list_open_gaps`,
`answer_gap` (the dialog) and the prompt `answer_gaps`. From `backend/`, with the layout above:

```bash
uv run --extra mcp python examples/mcp_gap_dialogs_demo.py
```

or with the single choice, for comparison:

```bash
uv run --extra mcp python examples/mcp_gap_dialogs_demo.py --layout select
```

```bash
claude mcp add --transport http gap-demo http://localhost:8011/mcp
```

Then, in an interactive Claude Code session, ask in your own words ("Which spec questions are open?"), or run
`/mcp__gap-demo__answer_gaps`. The result of `answer_gap` names the path the client took. Remove the server afterwards
with `claude mcp remove gap-demo`.

`client_supports_apps(ctx)` reads the client capabilities of the session. Our server is stateless, so a
`tools/call` request may carry none. The tools do not branch on it: they always return the full text and the UI
metadata.

## Security

| Threat | Mitigation |
|---|---|
| Text in a gap makes the model send an answer (prompt injection) | Write tools are app-only; text to the model is marked as data; `ui/update-model-context` carries ids and labels only. Optional: the answer ticket below |
| A view call acts as another user | Identity comes from the token only (AC9); the view never holds a token. The spike checks the identity of a call from the view |
| Script in document text | `textContent` only, the default content security policy, one self-contained bundle |
| No audit trail | The `answered` gap event gets `via` (`web` or `mcp_app`); `mcp.tool.completed` and `api.agent.written` as today |
| Remote connectors cannot reach the server | Calls of claude.ai and Desktop chat connectors come from Anthropic's backend, so `/mcp` must be reachable from the internet, and their OAuth redirect is not a loopback address, so they need a second client ([later.md](later.md) "A client id per MCP client type"). The Desktop Code tab runs Claude Code on the engineer's computer, so it uses the existing loopback client |
| Origin checks | Add the Claude sandbox origin (`<hash>.claudemcpcontent.com`) where the `Origin` check needs it (`MCP_ALLOWED_ORIGINS`) |

The answer ticket, in plain words: when the view opens a gap, the server puts a one-use ticket for that gap and that
user into the part of the reply that only the view reads (the result's `_meta`). `submit_gap_answer` accepts an answer only
with the ticket. When text in a gap tricks the model into calling `submit_gap_answer`, the call fails, because the model
never saw a ticket. This works only if the host does not give the result's `_meta` to the model. The spike checks
that; the decision follows (Q9).

## Try it locally

What the feature ships, prepared in this repository:

| Part | Where |
|---|---|
| The MCP role with the view's tools and the `ui://specflow/gaps` resource | `make up` builds and starts it as today: service `mcp` at `http://localhost:8002/mcp` ([deployment.md](deployment.md#local-stack)) |
| The view | One HTML file, built from a Vite entry under `frontend/` and put into the `mcp` image |
| Sign-in | The bundled Keycloak and the client `specflow-mcp` with the loopback redirect on port 8765, unchanged |
| Data | Gaps of a local project. A project needs a build and a gap run, which need model keys; the spike checks whether a seed of demo gaps is needed |
| A preview without Claude | The reference host `basic-host` of the ext-apps repository shows the view in a browser; the view's tests use it |

What the engineer sets up once on their computer:

1. Run `make env-local` and `make up` ([deployment.md](deployment.md#local-stack)).
2. Install the current Claude Desktop and open the Code tab on any folder.
3. Add the local server to Claude Code. The Code tab reads Claude Code's MCP settings (user scope or the project's
   `.mcp.json`), not the Desktop chat file `claude_desktop_config.json`; the spike confirms this:
   `claude mcp add --transport http --scope user --client-id specflow-mcp --callback-port 8765 specflow-local http://localhost:8002/mcp`
   ([deployment.md](deployment.md#local-endpoint)).
4. Sign in: `/mcp`, select `specflow-local`, Authenticate. The browser opens the local Keycloak; use
   `LOCAL_TEST_USER_EMAIL` and `LOCAL_TEST_USER_PASSWORD` from `.env.local`.
5. Ask "Show the open gaps of my project". Claude asks to show the app: Allow once. The view shows in the
   conversation.

No tunnel is needed: the Code tab runs on the engineer's computer and reaches `localhost`. Desktop chat and claude.ai
would need a tunnel or a public endpoint and a second OAuth client (Security).

## Delivery

| Phase | Content | Done when |
|---|---|---|
| P0 spike, 2 to 3 days | A hello-world view on our server. Check: the Desktop Code tab, Desktop chat and claude.ai; our sign-in on each; the identity of a call from the view; whether the model sees the result's `_meta`; Apps with the stateless server; the MCP settings that the Code tab reads | A support table with evidence |
| P1 browse | `browse_gaps`, `open_gap`, `get_gap`, the Browse and Answer screens; answers through `web_url` | The list and the card show in the target host; the CLI gets the text |
| P2 answer | Options, Other with preview, placement, reload, `suggest_gap_answer`, "Ask Claude"; AC12 changed in [access-control.md](access-control.md) | An answer from the view moves the gap to `answered` with `via` `mcp_app` |
| P3 terminal form | `answer_gap` with the `input_required` round trip, the confirm round for own answers, the shared `requestState` key | An answer from the form in the Claude Code CLI moves the gap to `answered` with `via` `elicitation` |

Deferred: assignment (D2), upload (D3).

## Changes by area

| Area | Change |
|---|---|
| Tool registry (`backend/src/mcp_server/registry.py`) | `ToolDeclaration` gets an optional UI binding (resource URI, visibility); `install` registers it through `mcp.server.apps.Apps` and serves the HTML as a `ui://` resource with `domain` and `csp` |
| API agent routes | Extend `GET /agent/gaps`; add `GET /agent/gaps/{gap_id}`, `POST .../edits_preview` and `POST .../answer` over the existing `GapService` calls, after the membership check |
| Gap events | `via` in the detail of the `answered` event |
| View | A Vite entry with React, `@modelcontextprotocol/ext-apps` and `vite-plugin-singlefile`; GD Liquid Glass tokens inlined; light and dark from the host context |
| Tests | Tool tests with a fake host; browser tests of the view in `basic-host`; manual checks in Claude Desktop |
| Docs | [tools.md](tools.md), [access-control.md](access-control.md) (AC12), [later.md](later.md), [../graph/gap/api.md](../graph/gap/api.md) "MCP tools" |

## Risks

| Risk | Level | Response |
|---|---|---|
| The CLI shows no views, and no date is known | High | The text path; check issue 95149 again before P1 |
| Claude surfaces differ (rollout, Code tab, organization flags) | Medium | The P0 support table; pinned SDK versions |
| The internal server is not reachable from Anthropic's backend | High for remote connectors | The Code tab target needs no public endpoint |
| Answers outside the web app change a written rule (AC12) | Medium | Change [access-control.md](access-control.md) before P2 |

## Open questions

| # | Question | Default until answered |
|---|---|---|
| Q8 | The target if the Code tab does not render views | Chosen with the spike's evidence |
| Q9 | Does `submit_gap_answer` require the answer ticket? | The spike checks whether the model sees the result's `_meta` |
| Q10 | Which dialog layout do people understand without help: one yes/no line per option, or the single choice? | One line per option ("Terminal dialogs"), until the next trial says otherwise |

## Deferred designs

Assignment (D2). Three ways were weighed: derived (gaps about the stories I hold, and gaps I raised), explicit (a
`gap_assignment` table with `take_gap` and `release_gap` that work like `claim_story`), or both. Its precondition, a
project with more than one member, exists since the project collaborators.

Upload (D3). Three meanings were weighed: the file becomes spec source through the existing corpus upload, the
view reads a text file into Other, or the file is an attachment for people only. Three transports were weighed:
chunked app-only tool calls under the 1 MiB request limit, a one-use upload URL (needs `connectDomains`), or a link to
the web app, which already uploads.

## Sources

- Specification 2026-01-26: https://github.com/modelcontextprotocol/ext-apps/blob/main/specification/2026-01-26/apps.mdx
- SEP-1865: https://modelcontextprotocol.io/seps/1865-mcp-apps-interactive-user-interfaces-for-mcp
- Announcement: https://blog.modelcontextprotocol.io/posts/2026-01-26-mcp-apps/
- SDK, examples and `basic-host`: https://github.com/modelcontextprotocol/ext-apps
- Quickstart: https://apps.extensions.modelcontextprotocol.io/api/documents/Quickstart.html
- Claude, MCP Apps: https://claude.com/docs/connectors/building/mcp-apps/getting-started
- Claude support, interactive connectors: https://support.claude.com/en/articles/13454812-use-interactive-connectors-in-claude
- Python SDK, Apps: https://py.sdk.modelcontextprotocol.io/advanced/apps/
- Claude Code, MCP (elicitation, client runtimes): https://code.claude.com/docs/en/mcp
- MCP 2026-07-28 changelog (multi-round-trip requests): https://modelcontextprotocol.io/specification/2026-07-28/changelog
- CLI request for views: https://github.com/anthropics/claude-code/issues/95149
- Elicitation: https://modelcontextprotocol.io/specification/2025-11-25/client/elicitation

Not verified (search results or reports by others only): the Code tab state, file pickers in the sandbox, and
elicitation in Claude Desktop and claude.ai.
