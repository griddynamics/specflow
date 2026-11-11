---
type: Requirements
title: "MCP server requirements"
description: "Requirement units of the MCP server (compact form): authentication, access control, tools, story and spec tools, workspace binding, web app guides, operations and non-functional"
status: stable
tags: [mcp]
---
# requirements.md: requirement units of the MCP server (compact form), functional and non-functional

Each row is one testable requirement in EARS form with one acceptance criterion. Status of every unit: Implemented,
except the units the MCP split adds or changes (FR-MCP-AUTH-13, FR-MCP-TOOL-06, FR-MCP-OPS-01, FR-MCP-OPS-02,
FR-MCP-OPS-07, NFR-MCP-03): Approved until the split is accepted ([implementation.md](implementation.md)); and the
units of the story and spec tools (FR-MCP-STORY, FR-MCP-READ, FR-MCP-WRITE, FR-MCP-START) with the changed
FR-MCP-TOOL-09: Approved until those tools are accepted.
Verification is by test unless the row says otherwise.

Each row states the behavior and links the section that defines it; values are defined only there. Acceptance criteria hold concrete test values.

## Authentication ([auth.md](auth.md))

| ID | Requirement | Acceptance |
|---|---|---|
| FR-MCP-AUTH-01 | In `oidc` mode, when a request to `/mcp` carries no bearer token, the service shall answer as [auth.md](auth.md#failure-responses) defines. | Given no `Authorization` header, then 401 and `resource_metadata="<origin of MCP_RESOURCE_URL>/.well-known/oauth-protected-resource/mcp"`. |
| FR-MCP-AUTH-02 | The service shall serve the protected resource metadata at the locations and from the sources [auth.md](auth.md#protected-resource-metadata) defines. | Given a request with `Host: other.example` in the allowlist, then both documents name `MCP_RESOURCE_URL` and `[OIDC_ISSUER]`. |
| FR-MCP-AUTH-03 | The service shall verify the token signature as checks 2 and 3 of [auth.md](auth.md#token-validation) define. | Given an HS256 token or one signed by an unknown key, then 401. |
| FR-MCP-AUTH-04 | The service shall check issuer and audience as checks 4 and 5 of [auth.md](auth.md#token-validation) define. | Given `aud=[MCP URL, account]`, then accepted; given `aud=specflow-api` or `aud=[MCP URL, specflow-api]`, then 401. |
| FR-MCP-AUTH-05 | The service shall check the client as check 8 of [auth.md](auth.md#token-validation) defines. | Given `azp=specflow-web`, then 401. |
| FR-MCP-AUTH-06 | If a valid token's email fails the email policy, then the service shall answer as [auth.md](auth.md#failure-responses) defines. | Given `carol@other.example`, then 403 `domain_not_allowed` and no `WWW-Authenticate` challenge. |
| FR-MCP-AUTH-07 | The token checks shall read the identity claims from the claim profile that [auth.md](auth.md#claim-profile-for-any-oidc-provider) defines, with its provider-neutral defaults. | Given `AUTH_TOKEN_TYPE=keycloak`, then every existing auth test passes unchanged; given the defaults, a token without `typ` and with only `client_id` is accepted while an ID token is still refused by the audience rule; given `AUTH_SUBJECT_CLAIM=oid`, then the `oid` value is the `Principal` subject; given `AUTH_TOKEN_TYPE=claim` with `AUTH_TOKEN_TYPE_CLAIM=x_token_kind` and `AUTH_TOKEN_TYPE_VALUE=access`, a token with `x_token_kind=access` is accepted while a token without the claim, with a non-string value or with another value gets 401. |
| FR-MCP-AUTH-08 | The REST API shall reject tokens issued for the MCP audience (S5 of [auth.md](auth.md#security-rules)). | Given a token with `aud=<MCP URL>`, when it calls `GET /api/v1/me`, then 401. |
| FR-MCP-AUTH-09 | The `mcp` role shall refuse to start on the settings that [architecture.md](architecture.md#settings) lists, including the audience mix-ups of [auth.md](auth.md#token-validation). | Given each refusal of both lists, then startup fails naming the variable; given `AUTH_TOKEN_TYPE=claim` and `MCP_AUDIENCE` equal to the client id, then startup succeeds. |
| FR-MCP-AUTH-10 | The service shall keep secrets and identity data out of logs and error bodies, as S2 of [auth.md](auth.md#security-rules) defines. | Given the full auth matrix, then captured logs and bodies contain no token, no `Authorization` value and no email. |
| FR-MCP-AUTH-11 | Locally, the role shall validate Keycloak tokens like any deployment ([auth.md](auth.md#local-mode)), and the API and the role shall treat one Keycloak user as one SpecFlow user. | Given alice creates a project through the REST API with her Keycloak web token, when an MCP client calls `list_projects` with her Keycloak MCP token, then the project is listed, both tokens carry the same `sub`, and her web token on `/mcp` gets 401. |
| FR-MCP-AUTH-12 | The `api` role shall refuse to start on the audience mix-ups that [auth.md](auth.md#token-validation) lists for it. | Given `AUTH_API_AUDIENCE=specflow-web` with the default client ids, then `specflow api --check-config` fails naming `AUTH_API_AUDIENCE`; given the same value under `AUTH_TOKEN_TYPE=claim`, the check passes. |
| FR-MCP-AUTH-13 | The API shall accept MCP-policy tokens only on its agent routes and API-policy tokens only on its other `/api/v1` routes, in both audience modes, as the audience matrix of [auth.md](auth.md#token-validation) defines. | In both modes (audience `MCP_RESOURCE_URL`; audience = the MCP client id with `AUTH_TOKEN_TYPE=claim`): an MCP token gets 200 on `GET /api/v1/agent/projects` and 401 or 403 on `GET /api/v1/projects`; a web token gets 200 on `GET /api/v1/projects` and 401 or 403 on every agent route; a smoke-client token gets 401 or 403 on every agent route; an ID token, a token with a tampered type claim, an expired token and no token get 401 on both. |

## Access control ([access-control.md](access-control.md))

| ID | Requirement | Acceptance |
|---|---|---|
| FR-MCP-ACC-01 | `list_projects` shall list only member projects (AC5, [access-control.md](access-control.md#decision-point)). | Given alice owns two projects and bob one, when alice lists, then bob's project is absent. |
| FR-MCP-ACC-02 | A tool with a `project_id` shall check membership first and run no graph query when it is denied (AC2 and AC4, [access-control.md](access-control.md#decision-point)). | API agent route tier: given a non-member, then a recording executor records zero queries. |
| FR-MCP-ACC-03 | A malformed, unknown or foreign project id shall be answered as AC3 of [access-control.md](access-control.md#decision-point) defines. | Given bob's existing project id with alice's token, a random UUID and `not-a-uuid`, then the three results are byte-identical and, in the API agent route tier, a recording executor records zero graph queries. |
| FR-MCP-ACC-04 | Membership shall be read on every call (AC6, [access-control.md](access-control.md#decision-point)). | API agent route tier (live Postgres) and E8: given the test deletes alice's `project_members` row in Postgres between two calls, then the second call answers `project_not_found`. |
| FR-MCP-ACC-05 | The caller's user row shall be provisioned or refreshed as AC1 of [access-control.md](access-control.md#decision-point) defines, keyed by the subject claim of the [claim profile](auth.md#claim-profile-for-any-oidc-provider). | Given a new subject, when it calls twice, then exactly one user row exists. |
| FR-MCP-ACC-06 | Every tool with a `project_id` shall pass through the one membership function (AC2, [access-control.md](access-control.md#decision-point)). | Inspection: the structural test reads the tool declarations and the agent routes, checks that every project-scoped tool maps to a route that runs the membership check, and passes. |

## Tools ([tools.md](tools.md))

| ID | Requirement | Acceptance |
|---|---|---|
| FR-MCP-TOOL-01 | `list_projects` shall page as [tools.md](tools.md#list_projects) defines. | Given 3 projects and `limit=2`, then pages of 2 and 1, the last with `next_cursor` null. |
| FR-MCP-TOOL-02 | `list_features` shall return the nodes of the type that [tools.md](tools.md#list_features) defines. | Given a project with features, modules and requirements, then only `FEAT::` ids are returned. |
| FR-MCP-TOOL-03 | `list_features` shall order and count as [tools.md](tools.md#list_features) defines. | Given 7 features and `limit=5`, then pages of 5 and 2, both with `total_count` 7. |
| FR-MCP-TOOL-04 | `list_features` shall read a page as [tools.md](tools.md#graph-read-used-by-list_features) defines. | Inspection: the page query's limit parameter is `limit + 1`; `GraphReader.nodes` is never called. |
| FR-MCP-TOOL-05 | Feature items shall hold the fields that [tools.md](tools.md#list_features) defines. | Given a feature with an embedding and provenance, then neither appears in the result. |
| FR-MCP-TOOL-06 | The feature label and id prefix shall come from the loaded ontology as [tools.md](tools.md#list_features) defines. | Given a copy of the shipped ontology whose `feature` type keeps its key but has another label and id prefix (for example `Funktion`, `FUNK::`), then `list_features` returns exactly that label's nodes; given an ontology without a `feature` type, then `list_features` answers `internal_error`, one log event names the failure and the API keeps running ([architecture.md](architecture.md#api-agent-routes)). |
| FR-MCP-TOOL-07 | If a cursor is invalid, then the tool shall answer with the error that [tools.md](tools.md#error-codes) defines for it (cursor rules in [tools.md](tools.md#conventions)). | Given a forged cursor and a 513-character cursor, then `invalid_cursor` for both tools. |
| FR-MCP-TOOL-08 | If Postgres or Neo4j fails, then the tool shall answer with the fixed error that [tools.md](tools.md#error-codes) defines. | Given Neo4j stopped, then `graph_unavailable` and no driver text. |
| FR-MCP-TOOL-09 | Every read tool shall declare the annotations that [tools.md](tools.md#conventions) defines (write tools: FR-MCP-WRITE-01). | Given `tools/list`, then `readOnlyHint` true, `destructiveHint` false, `idempotentHint` true and `openWorldHint` false on every tool except the five write tools. |
| FR-MCP-TOOL-10 | `GraphReader.count_nodes` and `nodes_page` shall count and page as [tools.md](tools.md#graph-read-used-by-list_features) defines (contract owner: [graph/api.md](../graph/api.md#read-and-search)). | Given 120 nodes, when paged by 50, then 50, 50, 20, `next_after` `None` at the end, count 120; `limit=0` raises `invalid_limit`. |

## Story and spec tools: people ([tools.md](tools.md#story-and-spec-tools))

Fixture: the shop plan of the graph library's test fixture, with alice and bob members of `P-Alpha`; bob's display
name is `Bob`.

| ID | Requirement | Acceptance |
|---|---|---|
| FR-MCP-STORY-01 | `list_areas` shall count each area's stories by [state](tools.md#story-states) and name the holders, as [tools.md](tools.md#list_areas) defines. | Given bob holds one Web shop story, when alice calls `list_areas`, then the Web shop row has `in_progress` `{"count": 1, "holders": [{"name": "Bob", "you": false, "agent_label": null}]}`, the counts add up to the area's active stories, and the project area is the last row. |
| FR-MCP-STORY-02 | `list_stories` shall list and order each view as [tools.md](tools.md#list_stories) defines. | Given two ready stories with `unblocks` 2 and 1 and the same release, one held, one waiting and one blocked story, then view `open` returns them in that order with `rank` 1 to 5; given alice holds A and her blocking finding blocked B, view `mine` returns A then B; given a completed story, view `done` returns it with its `merge_commit` and `completed_by.you` true. |
| FR-MCP-STORY-03 | `list_areas`, `list_stories` and `preview_story` shall put only the Markdown table of [tools.md](tools.md#tables) in the text block. | For each tool the text equals the render of the structured content; a title with a pipe and a line break gives one cell with the pipe escaped and the break as a space; with `limit` 2 of 5 stories the last line is `2 of 5 · next_cursor <the cursor>`; every other tool's text still parses to the structured content. |
| FR-MCP-STORY-04 | `preview_story` shall return the parts that [tools.md](tools.md#preview_story) defines and change nothing. | Given a story with four tasks and one blocker held by bob, then `items` are in topological order and the blocker's holder is Bob; after two calls the plan's nodes are unchanged. |
| FR-MCP-STORY-05 | Of concurrent claims of one story exactly one shall win, as [tools.md](tools.md#claim_story) defines. | Given alice and bob claim the same ready story at once, then one answer has a `claim_token` and the other is `story_claimed` with `details.holder`, `details.since` and at most 3 `ready` `alternatives` of the same area. |
| FR-MCP-STORY-06 | `claim_story` shall refuse a story it cannot claim with the code and details of [tools.md](tools.md#claim_story), and write nothing. | A waiting story gives `not_available` with `details.state` `waiting` and its blockers; a story with a task claimed alone gives `partly_claimed` with that `task_id`; a claim during a plan persist gives `plan_applying`; after each, the story and its tasks are unchanged. |
| FR-MCP-STORY-07 | A claim by the user who holds the story shall return the same token ([Repeats](tools.md#repeats)). | Given alice holds the story, when she calls `claim_story` again from a new session, then the token and the tasks equal the first answer and `api.agent.written` has `repeat` true. |
| FR-MCP-STORY-08 | `release_story` shall return the claim's tasks to `pending` and keep the note, as [tools.md](tools.md#release_story) defines. | Given alice's claim with one task reported `done`, when she releases with a note, then the tasks are `pending` without `progress` and the story keeps the note and branch; a second call answers `released: []`; bob's token answers `claim_mismatch`; a `branch` with a control character answers `invalid_arguments` and releases nothing. |
| FR-MCP-STORY-09 | The stories of one dependency cycle shall be claimed as one claim unit, as [Story states](tools.md#story-states) defines. | Given stories A and B whose tasks wait on each other, then `preview_story` of A has `story_ids` `[A, B]`; `claim_story` on A answers `story_ids` `[A, B]` and holds both with one token; `release_story` on B with that token releases the tasks of both. |

## Story and spec tools: agent reads ([tools.md](tools.md#story-and-spec-tools))

| ID | Requirement | Acceptance |
|---|---|---|
| FR-MCP-READ-01 | `get_story_brief` shall merge the briefs of the story's tasks, as [tools.md](tools.md#get_story_brief) defines, without a model call. | Given a story of three tasks sharing one implemented node, then `tasks` are in build order with their `done_when` sources, `implements` holds that node once, and a recording LLM gateway records no call. |
| FR-MCP-READ-02 | `trace` shall answer as [tools.md](tools.md#trace) and [../graph/trace.md](../graph/trace.md) define, and check its arguments before the library call. | Given the worked example of the trace design, then the nodes equal it and each carries `findings`; both or neither of `story_id` and `spec_ids` gives `invalid_arguments` and an unknown relation `unknown_relation`, with no library call. |
| FR-MCP-READ-03 | `get_node` shall bound and summarise edges as [tools.md](tools.md#get_node) defines. | Given a module with 60 domain edges and `edge_limit` 25, then `edges.items` has 25 entries, `edges.by_relation` counts add up to 60 and `edges.total` is 60; `include` `["edges"]` gives null `plan`, `provenance` and `findings`. |
| FR-MCP-READ-04 | `search_spec` shall rank hits near `near` first, as [tools.md](tools.md#search_spec) defines. | Given hits one and three hops from `near` and a higher-scored hit five hops away, then the first two hits have `hops_from_near` 1 and 3; without `near` the order equals the graph library's hybrid search; an unknown label gives `invalid_arguments`. |
| FR-MCP-READ-05 | `similar` shall return unlinked look-alikes above `min_score`, as [tools.md](tools.md#similar) defines. | Given a source with one linked and one unlinked node of score 0.9, then only the unlinked node is returned, with `like` naming the source; with `exclude_linked` false both are; `story_id` with `spec_ids` gives `invalid_arguments`. |
| FR-MCP-READ-06 | `get_evidence` shall return each evidence fragment with its context, as [tools.md](tools.md#get_evidence) defines. | Given a node built from lines 10 to 12 that a second node shares, with `context_lines` 2, then the item has the quote, lines 8 to 9 and 13 to 14 as context and the second node in `others`; an unknown id gives `node_not_found`. |
| FR-MCP-READ-07 | `path` shall return at most three shortest directed chains without a spine root inside, as [tools.md](tools.md#path) defines. | Given two nodes linked only through their module, then `items` is empty and `direction` null; given a dependency chain of two hops, then it is returned with `direction` `based_on`. |
| FR-MCP-READ-08 | `expand_brief` shall stay within the token budget, as [tools.md](tools.md#expand_brief) defines. | Given `budget_tokens` 500, then the items' `tokens` add up to `tokens` ≤ 500, no item is a node of the story or a brief neighbour, and two calls return the same answer. |

## Story and spec tools: agent writes ([tools.md](tools.md#story-and-spec-tools))

| ID | Requirement | Acceptance |
|---|---|---|
| FR-MCP-WRITE-01 | The five write tools shall declare the write annotations and call `POST` routes, as [tools.md](tools.md#conventions) and [architecture.md](architecture.md#write-routes) define. | `tools/list`: `readOnlyHint` false, `destructiveHint` false, `idempotentHint` true, `openWorldHint` false on exactly `claim_story`, `release_story`, `add_finding`, `report_task` and `complete_story`; the router sends `POST` with `project_id` in the query and the other arguments as the JSON body; the manifest has `read_only` false for exactly those five. |
| FR-MCP-WRITE-02 | A repeated write shall change nothing more and answer as [Repeats](tools.md#repeats) defines. | For each write tool, calling twice with the same arguments gives `200` both times, the graph after the second call equals the graph after the first, and the second `api.agent.written` has `repeat` true. |
| FR-MCP-WRITE-03 | A refused write shall write nothing (AC14 of [access-control.md](access-control.md#write-tools)). | For each write tool, given a foreign project, a stale token and invalid arguments, then the error code of [tools.md](tools.md#error-codes) and no change in the graph; the foreign project runs no graph query. |
| FR-MCP-WRITE-04 | `add_finding` shall store a finding beside the spec, as [tools.md](tools.md#add_finding) defines, and never change a spec node or edge. | Given a `gap` about two nodes with a commit reference, then one finding with `origin` `agent`, two `ABOUT` edges and one `REFERS_TO` edge exists and the spec nodes and edges are unchanged; the same call with the text in other case and spacing returns it with `created: false`; a `url` with user information gives `invalid_arguments`; with a recording HTTP client/socket guard, adding a `url` and a `pr` URL makes no network call. |
| FR-MCP-WRITE-05 | A blocking finding shall block only the caller's claimed story, as [tools.md](tools.md#add_finding) defines. | Kind `note` with `blocking` gives `invalid_arguments`; a node no task of the story implements gives `invalid_arguments`; bob's token gives `claim_mismatch`; a valid `question` sets the claim's tasks `blocked` with `spec_ambiguous` and `blocked_by`, ends the claim, and `list_stories` shows the story `blocked` with the question. |
| FR-MCP-WRITE-06 | `add_finding` shall refuse more than 50 open findings per caller and story. | Given 50 open findings by alice on a story, then the 51st gives `finding_limit`; after one is resolved, the next is stored. |
| FR-MCP-WRITE-07 | `report_task` shall record progress and never change the task status, as [tools.md](tools.md#report_task) defines. | Given a claimed task, `done` answers the task `in_progress` with `progress` `done`; `skipped` without a note gives `invalid_arguments`; a task of another member's claim gives `claim_mismatch`. |
| FR-MCP-WRITE-08 | `complete_story` shall complete the done tasks with the merge commit and release the rest, as [tools.md](tools.md#complete_story) defines. | Given a claim of three tasks and `done` of two, then those two are `completed` with the commit, `completed_by` alice and `completed_at`, the third is `pending` and in `released`, a story waiting only on them is in `now_available`, and the spec is unchanged; `merge_commit` `abc` gives `invalid_commit` and changes nothing. |
| FR-MCP-WRITE-09 | Error details shall be typed and leave today's errors unchanged, as [tools.md](tools.md#error-details) defines. | `story_claimed` carries details that validate against `ErrorDetails`; details that do not validate give `internal_error` with error type `ApiProtocolError`; the error goldens of the six earlier tools are byte-identical. |
| FR-MCP-WRITE-10 | No answer, log or error shall carry another user's id or email, a claim token other than the caller's, or finding text in logs ([access-control.md](access-control.md#write-tools)). | Given a finding text and an agent label holding a marker string, then the captured logs of both services hold neither the marker nor the token; given bob's display name equal to his email, his holder `name` is null; no answer of any story or spec tool holds a user id or an email. |

## Story and spec tools: guidance ([tools.md](tools.md#the-start-prompt))

| ID | Requirement | Acceptance |
|---|---|---|
| FR-MCP-START-01 | The server instructions shall end with the workflow line of [tools.md](tools.md#server-instructions) and stay under 2,048 characters for every valid resource URL. | Given `http://localhost:8002/mcp`, then the text ends with the workflow line and has 1,552 characters; given the longest valid URL (267 characters), then it is under 2,048. |
| FR-MCP-START-02 | The server shall offer the `start` prompt that [tools.md](tools.md#the-start-prompt) defines, behind the auth guard. | `prompts/list` lists `start` with an optional `area` argument; `prompts/get` with `area` `web` holds `"web"` as quoted data and the hand-off line; 201 characters are refused; without a token, 401. |
| FR-MCP-START-03 | From a bound project, the `start` prompt shall lead a coding agent to a claimed story in at most six tool calls. | Stack gate and agent QA: a headless Claude Code run of `start` on the isolated stack claims the story the simulated user chose, with at most six tool calls and no tool call the steps do not name. |

## Workspace binding ([tools.md](tools.md#workspace-binding))

| ID | Requirement | Acceptance |
|---|---|---|
| FR-MCP-BIND-01 | The `initialize` result shall carry the instructions that [tools.md](tools.md#server-instructions) defines. | Given `initialize`, then the instructions contain `MCP_RESOURCE_URL` and `.specflow/config.json`, are under 2,048 characters and contain no project id, name or feature text. |
| FR-MCP-BIND-02 | The server shall offer the prompt that [tools.md](tools.md#the-select_project-prompt) defines, behind the auth guard. | Given a valid token, then `prompts/list` lists `select_project` with an optional `project` argument, and `prompts/get` returns the steps and the exact version 1 JSON, with `project` `Checkout` kept as quoted data; given no token, then 401. |
| FR-MCP-BIND-03 | After `select_project`, the folder shall hold a binding file that [tools.md](tools.md#the-binding-file) defines and that names the chosen project. | Agent tier step 1: the file validates against the contract schema and names P-Alpha. |
| FR-MCP-BIND-04 | A new session in a bound folder shall apply rule 1 of [tools.md](tools.md#rules). | Agent tier step 2: `list_features` is called with P-Alpha's id and `list_projects` is never called. |
| FR-MCP-BIND-05 | Selecting again shall replace the whole file, as [tools.md](tools.md#rules) defines, and the next session shall use the new project. | E17 and agent tier step 4: the file names P-Empty and the next session lists features of P-Empty (`total_count` 0). |
| FR-MCP-BIND-06 | A binding to a project the caller is not a member of shall be answered as rule 4 of [tools.md](tools.md#rules) defines. | E17 and agent tier step 3: a file bound to P-Beta answers `project_not_found`, no P-Beta feature name appears, and no other project is used. |
| FR-MCP-BIND-07 | Project tools shall keep a required `project_id`, and the binding shall add no server state ([tools.md](tools.md#workspace-binding)). | `tools/list` input schemas are unchanged except descriptions; no new table or setting. |

## Web app guides ([deployment.md](deployment.md#guides-in-the-web-app))

| ID | Requirement | Acceptance |
|---|---|---|
| FR-MCP-UI-01 | The web app shall serve the pages that [deployment.md](deployment.md#guides-in-the-web-app) defines, reachable from a Help section at the bottom of the sidebar with or without an open project; any other topic shall answer 404. | Playwright: Guides shows on the home page and inside a project; `/guides`, `/guides/specflow-101`, `/guides/best-practices` and `/guides/mcp-server` open; `/guides/nope` answers 404. |
| FR-MCP-UI-02 | The `/guides` hub shall show one card per topic under a header that is the "Guides" breadcrumb (no separate title block), and each topic page shall show "Guides › topic" in the same place, linking back to the hub. | Playwright: three cards; the hub's breadcrumb reads "Guides" on the project picker's row; from `/guides/mcp-server` the breadcrumb link "Guides" opens `/guides`. |
| FR-MCP-UI-03 | The MCP server guide shall build its commands and files from the address and client id of the [web app settings](deployment.md#web-app-settings-for-the-guides) (`MCP_PUBLIC_URL`, `MCP_CLIENT_ID`), with their defaults, never a placeholder. | Unit: with neither setting and `PUBLIC_URL=https://specflow.example.com`, the address is `https://specflow.example.com/mcp` and the client id `specflow-mcp`; a blank `MCP_CLIENT_ID` and an `ftp://` `MCP_PUBLIC_URL` are refused. Playwright with `MCP_PUBLIC_URL=https://mcp.example.com/mcp` and `MCP_CLIENT_ID=specflow-test`: both appear in the step-1 command. |
| FR-MCP-UI-04 | Every snippet of the guides shall copy exactly its text with one click. | Playwright with clipboard permission: after the COPY button of each block, the clipboard equals the block's text. |
| FR-MCP-UI-05 | Only tested agents shall show setup steps; the others shall show "Setup is in progress", and `?agent=` shall select the agent ([deployment.md](deployment.md#guides-in-the-web-app)). | Playwright: `?agent=claude-code` shows four steps; `?agent=codex` and `?agent=cursor` show the empty state and no step. |
| FR-MCP-UI-06 | The tools table of the guide shall come from the registry's manifest ([architecture.md](architecture.md#tool-registry)), and a stale committed manifest shall fail a test. | Backend test: after a planted change to a tool summary, the comparison fails and names `make mcp-manifest`. Playwright: the table lists every name of the manifest. |
| FR-MCP-UI-07 | Plan › Develop shall show, per project, the bind command with the project id, the binding file that validates against the contract ([tools.md](tools.md#the-binding-file)), and the link to the guide; no mock content shall remain. | Unit: for a project named `Checkout "v2"` and for a non-ASCII name, the built JSON validates against `specflow-workspace-config.schema.json`. Playwright: the command holds the project's id. |
| FR-MCP-UI-08 | The MCP guide and Plan › Develop shall use the server name of `MCP_SERVER_NAME` (default `specflow`), with the same steps in every deployment ([deployment.md](deployment.md#guides-in-the-web-app)). | Unit: with `MCP_SERVER_NAME=specflow-local` the commands name `specflow-local` and `/mcp__specflow-local__select_project`; without it, `specflow`. Local stack check: the guide shows `http://localhost:8002/mcp` and `specflow-local`. |
| FR-MCP-UI-09 | The guide pages shall follow the design-system rules and be usable by keyboard and at phone width. | `npm run check:tokens` passes; review against the design system; Playwright at 390 px width: no horizontal page scroll and every control reachable by Tab. |

## Operations ([architecture.md](architecture.md), [deployment.md](deployment.md))

| ID | Requirement | Acceptance |
|---|---|---|
| FR-MCP-OPS-01 | The `mcp` role shall start with only the settings that [architecture.md](architecture.md#settings) names for it, and shall read no database or graph store setting. | Given no `DATABASE_*`, `GRAPH_STORE_*`, `NEO4J_*`, Temporal, storage or LLM variable, then `specflow mcp --check-config` succeeds; on the local stack the `mcp` container's environment holds no `DATABASE_` or `GRAPH_STORE_` name. |
| FR-MCP-OPS-02 | `/readyz` shall report readiness from the API's `/readyz`, with the same `database` and `graph` checks, as [architecture.md](architecture.md#components) (Readiness) defines. | Given the graph store stopped, then 503 with `graph` not ok; given the API stopped, then 503 with `database` and `graph` not ok and detail `api_unreachable`. |
| FR-MCP-OPS-03 | The service shall not need session affinity ([architecture.md](architecture.md#transport), Sessions). | Given two app instances, when `initialize` goes to one and `tools/call` to the other, then the call succeeds. |
| FR-MCP-OPS-04 | The service shall refuse a `Host` and an `Origin` as [architecture.md](architecture.md#transport) defines. | Given `Host: evil.example` or `Origin: https://evil.example`, then refused. |
| FR-MCP-OPS-05 | The service shall refuse oversized request bodies as [architecture.md](architecture.md#transport) defines. | Given a 2 MiB body, then 413. |
| FR-MCP-OPS-06 | An unknown path shall be answered as [architecture.md](architecture.md#http-surface-of-the-role) defines. | Given `GET /nope`, then 404 `not_found`. |
| FR-MCP-OPS-07 | Every tool call that passes the input schema shall be audited as [architecture.md](architecture.md#logging-and-audit) defines, whatever the outcome, with the user id taken from the API's `X-SpecFlow-User-Id` response header and omitted when that header is absent. | Given one call of each tool and one `list_features` call for a foreign project, then one event per call with user id, tool, project id, outcome, item count, duration and request id (the last with outcome `project_not_found`) and no feature text; given an API `2xx` without the header, then the event has no user id, outcome `internal_error` and error type `ApiProtocolError`; given a caller-sent `X-SpecFlow-User-Id`, then it is not forwarded to the API. |
| FR-MCP-OPS-08 | `make up` shall start the `mcp` role and Keycloak as [deployment.md](deployment.md#local-stack) defines. | Given a fresh `.env.local` from `make env-local`, then both services are healthy, the PRM names the local issuer, and a Keycloak token for the user of the file is accepted on `/mcp`; given a file without a Keycloak value, `make up` stops before compose and names it. |

## Non-functional

The targets below are the only place the quality numbers are defined; other files link here.

| ID | Requirement | Acceptance |
|---|---|---|
| NFR-MCP-01 | Tool calls shall answer within 500 ms at the 95th percentile at 10 calls per second on the local stack against a project with 300 features. | Measured by end-to-end scenario E14 of [testing.md](testing.md#end-to-end-scenarios) (`P-Large`, 60 s) and recorded in its report. |
| NFR-MCP-02 | The `mcp` SDK shall be a declared dependency in the range that [architecture.md](architecture.md#transport) defines, and the agent suites shall pass with the updated lock. | `make check` passes after the lock update. |
| NFR-MCP-03 | The import rules of [architecture.md](architecture.md#import-rules) shall be import-linter contracts on direct imports, and the Cypher and SQL validation tracks shall flag query text in the API agent routes. | `make validate-python-lint`, `make validate-cypher` and `make validate-sql` pass; the lint track fails on a planted `db` import in `mcp_server`, and the Cypher and SQL tracks each fail on a planted violation in `api.routers.agent`. |
| NFR-MCP-04 | The repository's static validation shall pass. | `make validate` passes. |
| NFR-MCP-05 | No secret shall be committed; the local stack keeps secrets as [deployment.md](deployment.md#local-stack) defines. | No tracked file holds a secret value of `.env.local` (checked by a scan that prints no values); the realm file has no `credentials` or `secret` value. |
