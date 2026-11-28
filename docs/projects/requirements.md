---
type: Requirements
title: "Project collaborators requirements"
description: "Requirement units of project collaborators (compact form): roles, access, members and invites, adoption, removal and claims, rename, suggestions, Create Project, Members UI, non-functional"
status: stable
tags: [projects]
---
# requirements.md: requirement units of project collaborators (compact form), functional and non-functional

Each row is one testable requirement in EARS form with one acceptance criterion. Status of every unit: implemented
(2026-11-17). Verification is by test unless the row says otherwise. The rows state behavior
and link the section that defines it; values live only there. Decisions (D1-D29) are in [questions.md](questions.md).

## Roles ([data-model.md](data-model.md#roles))

| ID | Requirement | Acceptance |
|---|---|---|
| FR-PRJ-ROLE-01 | When a user creates a project, the API shall make that user its `owner` (D1). | Given `POST /projects`, then one member row with the caller and role `owner`. |
| FR-PRJ-ROLE-02 | The database shall refuse a second `owner` row in a project (D1). | Given a project with an owner, when another `owner` row is inserted, then a unique violation. |
| FR-PRJ-ROLE-03 | The schema shall allow only the roles `owner` and `collaborator`, with no default role (D31, D33). | Given a database at the head revision, inserting `role='editor'` or an insert without a role fails. |

## Access ([api.md](api.md#access))

| ID | Requirement | Acceptance |
|---|---|---|
| FR-PRJ-ACC-01 | Every member, owner or collaborator, shall have the same access to the project on web and agent routes (D21). | Given a collaborator, then `GET /projects/{pid}` is 200 and the MCP `list_projects` lists the project. |
| FR-PRJ-ACC-02 | When a collaborator calls an owner route, the API shall answer 403 `owner_required` and change nothing. | Given a collaborator, `PATCH /projects/{pid}`, then 403 and the name is unchanged. |
| FR-PRJ-ACC-03 | When a non-member calls any route of this spec with a `pid`, the API shall answer 404 `project_not_found`. | Given a non-member, `GET /projects/{pid}/members`, then 404 `project_not_found`. |
| FR-PRJ-ACC-04 | When a member is removed or leaves, the API shall refuse their next request to the project, web or agent (D20). | Given a removed collaborator, then `GET /projects/{pid}` and the agent route `GET /agent/features` both answer 404. |

## Members and invites ([api.md](api.md#add-a-member-or-invite))

| ID | Requirement | Acceptance |
|---|---|---|
| FR-PRJ-MEM-01 | The API shall list a project's members and invites with the caller's role, in the order of [api.md](api.md#shapes) (D4, D7). | Given owner A, collaborator B and invite c, then `role` is the caller's and the lists are `[A, B]`, `[c]`. |
| FR-PRJ-MEM-02 | When the owner adds an email of a user who signed in, the API shall add that user as a `collaborator` (D2). | Given user `Bob@Example.com`, add `bob@example.com`, then 201 `kind: "member"`. |
| FR-PRJ-MEM-03 | When the owner adds an email no user has, the API shall store a pending invite (D5). | Given an unknown email, then 201 `kind: "invite"` and one `project_invites` row. |
| FR-PRJ-MEM-04 | The API shall compare emails normalised (D2). | Given an invite for `x@example.com`, adding ` X@Example.COM ` answers 409 `already_invited`. |
| FR-PRJ-MEM-05 | If an added email has a domain outside the allowed sign-in domains, then the API shall answer 422 `email_domain_not_allowed` (D11). | Given allowed `example.com`, adding `a@other.org`, then 422 and no row. |
| FR-PRJ-MEM-06 | If an add would exceed the limit, then the API shall answer 409 `member_limit` (D19). | Given 50 members and invites, the 51st add answers 409; given 49, two concurrent adds give one 201 and one 409. |
| FR-PRJ-MEM-07 | If the email is already a member or invited, then the API shall answer 409 `already_member` or `already_invited`. | Given the owner's own email, then 409 `already_member`. |
| FR-PRJ-MEM-08 | When the owner removes a collaborator, the API shall delete the member row; it shall refuse to remove the owner (`cannot_remove_owner`). | Given B, then 200 and B is gone; given the owner's id, then 409. |
| FR-PRJ-MEM-09 | When the owner cancels an invite, the API shall delete it (D5). | Given invite c, then 204; a later sign-in with that email adopts nothing. |
| FR-PRJ-MEM-10 | When a collaborator leaves, the API shall delete their member row; it shall refuse the owner (`owner_cannot_leave`) (D13). | Given B, `POST /leave`, then 200 and B is gone; given the owner, 409. |
| FR-PRJ-MEM-11 | An invite shall stay pending until adopted or cancelled (D19). | Inspection: no expiry column or job. |

## Adoption ([data-model.md](data-model.md#invite-adoption))

| ID | Requirement | Acceptance |
|---|---|---|
| FR-PRJ-INV-01 | When a user whose normalised email has invites makes any accepted request, the API shall turn each invite into a `collaborator` row (D5, D27). | Given invites in two projects for `c@example.com`, then after `GET /me` with `C@example.com` the user is a collaborator of both and no invite remains. |
| FR-PRJ-INV-02 | Adoption shall run on the agent routes as on the web routes (D21). | Given an invite, the first call is the agent `GET /agent/projects`, then it lists the project. |
| FR-PRJ-INV-03 | Concurrent requests of one user shall adopt each invite once. | Given two sessions adopting at once, then one member row, no error. |
| FR-PRJ-INV-04 | Adoption shall also adopt an invite created after the user's first sign-in. | Given user U signed in, then an invite row for U's email inserted directly, then U's next request adopts it. |

## Removal and claims ([api.md](api.md#remove-leave-and-claims))

| ID | Requirement | Acceptance |
|---|---|---|
| FR-PRJ-CLM-01 | When a collaborator is removed or leaves, the API shall force-release every story claim they hold, after their membership is gone (D12, D17). | Given B holding two story claims, then `claims_released: 2` and the claims board lists none of B's. |
| FR-PRJ-CLM-02 | If the graph store fails while releasing, then the removal shall stand and the answer shall say `claims_release_failed: true`. | Given a failing plan read, then 200, the member row is gone, `claims_release_failed: true`. |
| FR-PRJ-CLM-03 | When the project has no plan, the release shall report 0 and no failure. | Given no plan, then `claims_released: 0`, `claims_release_failed: false`. |
| FR-PRJ-CLM-04 | The removal shall keep the person's documents, findings and history (D20). | Given B's finding, after removal it is listed with a null author name. |

## Rename ([api.md](api.md#rename))

| ID | Requirement | Acceptance |
|---|---|---|
| FR-PRJ-NAME-01 | When the owner renames the project, the API shall store the new name under the Create Project name rule (D6). | Given `"  New  "`, then 200 `name: "New"`; given 201 code points, then 422. |

## Suggestions ([api.md](api.md#suggestions))

| ID | Requirement | Acceptance |
|---|---|---|
| FR-PRJ-SUG-01 | When a signed-in user sends at least 3 characters, the API shall return up to 8 signed-in users whose email, name or a name word starts with them (D9, D10). | Given 10 users `ann1..ann10@example.com`, `q="ann"`, then 8 items; `q="an"`, then 422. |
| FR-PRJ-SUG-02 | The API shall exclude the caller, users outside the allowed domains and, with `project_id`, its members and invited emails. | Given member B and invite c, `q` matching both, then neither is returned. |
| FR-PRJ-SUG-03 | Suggestions shall carry name and email only. | Given any result, then items have exactly `name` and `email`. |
| FR-PRJ-SUG-04 | The API shall treat `%` and `_` in `q` as plain characters. | Given `q="a_b"`, then `axb@example.com` is not returned. |

## Create Project ([api.md](api.md#create-with-collaborators))

| ID | Requirement | Acceptance |
|---|---|---|
| FR-PRJ-CRT-01 | When Create Project carries collaborator emails, the API shall add members and invites under the rules of this spec in the same transaction (D8). | Given one signed-in and one unknown email, then 201, one collaborator row and one invite. |
| FR-PRJ-CRT-02 | If any collaborator email fails a rule, then the API shall create nothing and answer 422 naming the index. | Given `["ok@example.com", "bad"]`, then 422 with `loc: ["body","collaborators",1]` and no project. |
| FR-PRJ-CRT-03 | When `collaborators` is absent or empty, Create Project shall behave as before. | Given `{name}`, then 201 and one owner row. |

## Members UI ([ui.md](ui.md))

| ID | Requirement | Acceptance |
|---|---|---|
| FR-PRJ-UI-01 | The V2 header shall offer "Members", opening the current project's Members dialog over the page (D11). | Given project P current, clicking Members opens the dialog over the screen. |
| FR-PRJ-UI-02 | The Members dialog shall need no route or tab of its own; the V2 header shall hold the project menu, Members and Integrations, and a screen body no H1 or tab row (D16, D25). | Given any V2 screen, then the header holds Members and Integrations and the body has no `h1`. |
| FR-PRJ-UI-03 | For the owner, the Members dialog shall show name + Save, the add row with suggestions, and the list with x on every row but the owner's (D24). | Given the owner, then these controls are present and the owner row has no x. |
| FR-PRJ-UI-04 | For a collaborator, the Members dialog shall show the read-only list and "Leave project", without the name block, the add row or any x (D7, D29). | Given a collaborator, then no textbox, no Add, no x; "Leave project" present. |
| FR-PRJ-UI-05 | Remove, cancel and leave shall ask for confirmation first (D28). | Given x, then a dialog; Cancel sends no request. |
| FR-PRJ-UI-06 | The Members dialog shall show the count "n of 50" (D28). | Given 3 members and 1 invite, then "4 of 50". |
| FR-PRJ-UI-07 | When the owner renames the project, the project menu shall show the new name without a reload. | Given Save, then the menu shows the new name. |
| FR-PRJ-UI-08 | When a collaborator leaves, the dialog shall close and the app shall open `/`, and the project menu shall no longer list the project. | Given Leave confirmed, then URL `/` and the project is not in the menu. |
| FR-PRJ-UI-09 | The Create Project dialog shall take the name only; collaborators are invited from Members once the project exists (D11). The Create Project port shall still send `collaboratorEmails` when a caller sets them (D8, D9). | Given the dialog's Create, then the POST body is `{name}`; given the http port with two emails, `{name, collaborators: [a, b]}`. |
| FR-PRJ-UI-10 | Errors shall show as [ui.md](ui.md#errors-shown) defines. | Given 409 `already_invited`, then "Already invited." under the field. |

## Non-functional

| ID | Requirement | Acceptance |
|---|---|---|
| NFR-PRJ-01 | Log events of this spec shall carry ids, counts and outcomes only, never an email, a name or the suggestion text (D22, D26). | Given every route exercised under the log harness, then no record holds `@` or the query text. |
| NFR-PRJ-02 | No route of this spec shall carry an email in its URL. | Inspection: paths and query strings of [api.md](api.md#routes). |
| NFR-PRJ-03 | No notification shall be sent (D3). | Inspection: no mail or message client is called. |
| NFR-PRJ-04 | The access decision shall stay one membership query per project-scoped request. | Given the db recorder, a project route issues one membership query. |
| NFR-PRJ-05 | `project_invites` shall have row-level security with the `agent_project_scope` policy and no agent grant. | Given the offline upgrade SQL, then RLS is enabled and the policy exists for it, and the only GRANT stays the documents one. |
| NFR-PRJ-07 | The API contract snapshot and the frontend contract list shall match the routes. | `make validate` passes with the new routes. |
