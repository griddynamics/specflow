---
type: Design
title: "Notion codes and limits"
description: "The closed set of notion_* codes: where each is used, the API error each becomes, limits and what is not verified live"
status: draft
tags: [notion, codes]
---
# codes.md: the closed `notion_*` codes

`NotionCode` (`notion.reasons`) is the one closed set. `NotionError` carries exactly one code and nothing else: no Notion
text, token, code, `state`, page title or workspace name is ever inside an error, a log line or an API answer. A code is an
API error code (`api.notion` turns it into an `ApiError` with fixed text), a run code of the plan destination, or both.

| Code | HTTP | Where | Meaning |
|---|---|---|---|
| `notion_unavailable` | 503 | every route | the integration is off on this server (`NOTION_OAUTH_CLIENT_ID` unset) |
| `notion_not_connected` | 409 | check, save, pass, connection | no Notion connection, or no Notion binding on the project |
| `notion_reconnect_required` | 409 | check, save, pass | the member's connection expired (a dead refresh token, a rotated `NOTION_TOKEN_KEY`) |
| `notion_state_invalid` | 400 | callback | the sign-in `state` expired, is forged or belongs to another user |
| `notion_code_invalid` | 400 | callback | Notion did not accept the authorisation code |
| `notion_unreachable` | 502 | all | Notion down, the network failed or the retries ran out |
| `notion_auth_failed` | 502 | all | Notion refused this server's credentials or a fresh token (a setup problem); also the answer for any code without its own |
| `notion_link_invalid` | 422 | source check and add, plan check and save | the text is not a link to a Notion page or database |
| `notion_not_shared` | 409 | source check and add, plan check and save, item | no such page or database, or the member did not tick it in the page picker (Notion answers both with 404) |
| `notion_parent_not_a_page` | 422 | plan check and save | the parent link is a database; the plan database needs a page |
| `notion_permissions_missing` | 409 | plan check, save, pass | the connection lacks a capability (insert or update content, Notion's 403) |
| `notion_rate_limited` | run code | plan pass | the pass waited its 60 s budget for the request limit |
| `notion_page_gone` | run code, item | plan pass | a person deleted or archived a row the export made |
| `notion_database_gone` | run code | plan pass | a person deleted or archived the plan database |
| `notion_item_invalid` | run code, item | plan pass | Notion refused the content of one row (400) |
| `notion_grant_invalid`, `notion_refused` | inside only | connections | a dead refresh token (becomes `notion_reconnect_required`); any other refusal of the token endpoint (becomes `notion_code_invalid` in the sign-in, `notion_auth_failed` elsewhere) |

Other run codes of the pass (not `notion_*`): `notion_plan_not_configured`, `no_plan`, `plan_applying`, `target_changed`,
`unexpected_error`, `interrupted`.

Imports keep the closed failure reasons of sources (`no_access`, `convert_failed`, `unknown_error`, N6); they never show a
`notion_*` code. The check route does.

## Limits

| Limit | Value | Where |
|---|---|---|
| Notion request limit | 180 a minute (Free, Plus), 600 (Business, Enterprise); `Retry-After` at most 60 s | [oauth.md](oauth.md#request-budget) |
| Our rate | `NOTION_REQUESTS_PER_MINUTE`, default 150, one limiter per bot, import and export together | [oauth.md](oauth.md#request-budget) |
| Retries | `429` and `529` always, `5xx` for `GET` only, five attempts, exponential backoff with jitter | [oauth.md](oauth.md#request-budget) |
| Wait of one pass or import | 60 s in all | [plan.md](plan.md#rules-of-a-pass-n8-n9) |
| Pages of one import | 100, pages and rows together | [source.md](source.md#caps-n4) |
| Rows written by one pass | 120, the rest in follow-up passes | [plan.md](plan.md#row-cap-n9) |
| Link length | 2048 characters | `NotionLinkIn` |

## Not verified live

Everything here was designed against Notion's documentation and a sandbox read with an internal connection. The first live
checks are left to the owner: the OAuth consent and the page picker (needs a browser click), the 600-a-minute limit of
Business workspaces, a `truncated` page, and a plan export of several hundred rows. See the "Not verified live" section of
each file.
