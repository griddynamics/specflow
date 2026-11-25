// Browser-side client for the BFF (/api/backend/* -> API /api/v1/*). Every call is
// same-origin: the session cookie is HttpOnly and tokens never reach this code.

import { PAGE_LIMIT } from "@/lib/domain/source-state";
import { contentTypeFor } from "@/lib/domain/upload-formats";

export interface Project {
  id: string;
  name: string;
  created_at: string;
}

export interface DocumentSummary {
  id: string;
  original_filename: string;
  size_bytes: number;
  status: "uploaded" | "processing" | "parsed" | "failed";
  error: string | null;
  summarization_status: "none" | "running" | "succeeded" | "failed";
  created_at: string;
}

export interface Page<T> {
  items: T[];
  next_cursor: string | null;
}

export interface ValidationEntry {
  loc: (string | number)[];
  type: string;
}

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string = code,
    /** `details.max_bytes` of a 413 `file_too_large`: the cap of that file type. */
    readonly maxBytes?: number,
    /** `details.errors` of a 422 `validation_failed`: where (`loc`) and which rule (`type`); never a submitted value. */
    readonly errors?: ValidationEntry[],
    /** The envelope's `details` (e.g. `field` and `reason` of a 400 `answer_invalid`, `status` of a 409 `gap_wrong_state`). */
    readonly details?: Readonly<Record<string, unknown>>,
  ) {
    super(message);
  }
}

export const UNAUTHENTICATED_EVENT = "specflow:unauthenticated";
const BASE = "/api/backend";
const HEADERS = { "X-Requested-With": "specflow" };

export interface ParsedEnvelope {
  body: { error?: { code?: unknown; details?: { max_bytes?: unknown; errors?: unknown } & Record<string, unknown> } } | null;
  parsed: boolean;
}

/**
 * Reads a backend response: announces a 401 on `UNAUTHENTICATED_EVENT` (the shell navigates to /login) and parses the JSON body.
 * An ingress or the Next error page can answer with HTML or plain text: it is never parsed blindly and never shown (`parsed` is false).
 */
export async function readEnvelope(res: Response): Promise<ParsedEnvelope> {
  if (res.status === 401) {
    window.dispatchEvent(new Event(UNAUTHENTICATED_EVENT));
  }
  const text = await res.text();
  let body: ParsedEnvelope["body"] = null;
  let parsed = true;
  if (text) {
    try {
      body = JSON.parse(text) as ParsedEnvelope["body"];
    } catch {
      parsed = false;
    }
  }
  return { body, parsed };
}

/** `details.errors` of an error envelope: the entries of the shape `{loc, type}`; anything else is dropped. */
function validationEntries(raw: unknown): ValidationEntry[] | undefined {
  if (!Array.isArray(raw)) return undefined;
  const out: ValidationEntry[] = [];
  for (const e of raw as unknown[]) {
    if (typeof e !== "object" || e === null) continue;
    const { loc, type } = e as { loc?: unknown; type?: unknown };
    if (!Array.isArray(loc) || typeof type !== "string") continue;
    out.push({ loc: (loc as unknown[]).filter((p): p is string | number => typeof p === "string" || typeof p === "number"), type });
  }
  return out;
}

/** A same-origin GET of `/api/backend<path>`: the raw response, with the CSRF header and the session cookie. */
export const getBackend = (path: string): Promise<Response> =>
  fetch(`${BASE}${path}`, { method: "GET", credentials: "same-origin", headers: HEADERS });

/** POST to the BFF's own `/api/auth/end-session`: where the browser goes to finish a sign-out, or undefined when none came back. */
export const endSessionDestination = (): Promise<unknown> =>
  fetch("/api/auth/end-session", { method: "POST", cache: "no-store" })
    .then(async (res) => (res.ok ? ((await res.json()) as { url?: unknown }).url : undefined))
    .catch(() => undefined);

async function call(path: string, init: RequestInit = {}): Promise<{ status: number; body: unknown }> {
  const headers = new Headers(HEADERS);
  new Headers(init.headers).forEach((value, key) => {
    headers.set(key, value);
  });
  const res = await fetch(`${BASE}${path}`, {
    ...init,
    credentials: "same-origin",
    headers,
  });
  const { body, parsed } = await readEnvelope(res);
  if (!res.ok) {
    const code = parsed && typeof body?.error?.code === "string" ? body.error.code : `http_${String(res.status)}`;
    const details = body?.error?.details;
    const maxBytes = details?.max_bytes;
    if (parsed && code === body?.error?.code)
      throw new ApiError(
        res.status,
        code,
        code,
        typeof maxBytes === "number" ? maxBytes : undefined,
        validationEntries(details?.errors),
        details ?? undefined,
      );
    throw new ApiError(res.status, code, `Request failed (HTTP ${String(res.status)}). Please try again.`);
  }
  if (!parsed) throw new ApiError(res.status, "invalid_response", `Unexpected response from the server (HTTP ${String(res.status)}).`);
  return { status: res.status, body };
}

// One helper per HTTP method, so every call site reads `METHOD path` and maps to exactly one endpoint of the API contract
// (backend/contracts/openapi.json, checked against frontend/validation/api-contracts.json). Paths are written in full at the call.
const httpGet = (path: string) => call(path);
const httpPost = (path: string, init: RequestInit = {}) => call(path, { ...init, method: "POST" });
const httpPatch = (path: string, init: RequestInit = {}) => call(path, { ...init, method: "PATCH" });
const httpPut = (path: string, init: RequestInit = {}) => call(path, { ...init, method: "PUT" });
const httpDelete = (path: string) => call(path, { method: "DELETE" });

const json = (value: unknown): RequestInit => ({
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify(value),
});

export const listProjects = () => httpGet("/projects?limit=100").then((r) => r.body as Page<Project>);
/** `{name}`, or `{name, collaborators}` when `collaborators` holds at least one email (api.md#create-with-collaborators). */
export const createProject = (name: string, collaborators: string[] = []) =>
  httpPost("/projects", json(collaborators.length > 0 ? { name, collaborators } : { name })).then((r) => r.body as Project);
export const renameProject = (pid: string, name: string) =>
  httpPatch(`/projects/${encodeURIComponent(pid)}`, json({ name })).then((r) => r.body);
export const getProject = (pid: string) => httpGet(`/projects/${encodeURIComponent(pid)}`).then((r) => r.body as Project);

// Members, invites, leave and suggestions (docs/projects/api.md#routes). Bodies stay `unknown`; the members port
// (lib/data/http/members.ts) checks their shape. No email ever goes into a path or query: ids in paths, emails and the typed
// suggestion text in POST bodies (NFR-PRJ-02).
export const listMembers = (pid: string) => httpGet(`/projects/${encodeURIComponent(pid)}/members`).then((r) => r.body);
export const addMember = (pid: string, email: string) =>
  httpPost(`/projects/${encodeURIComponent(pid)}/members`, json({ email })).then((r) => r.body);
export const removeMember = (pid: string, userId: string) =>
  httpDelete(`/projects/${encodeURIComponent(pid)}/members/${encodeURIComponent(userId)}`).then((r) => r.body);
/** 204: resolves with the HTTP status. */
export const cancelInvite = (pid: string, inviteId: string) =>
  httpDelete(`/projects/${encodeURIComponent(pid)}/invites/${encodeURIComponent(inviteId)}`).then((r) => r.status);
export const leaveProject = (pid: string) => httpPost(`/projects/${encodeURIComponent(pid)}/leave`).then((r) => r.body);
export const suggestUsers = (q: string, projectId?: string) =>
  httpPost("/users/suggestions", json(projectId === undefined ? { q } : { q, project_id: projectId })).then((r) => r.body);

// The Slack channel of a project (docs/slack/gap-answering.md "The channel flow"). Bodies stay `unknown`; lib/data/http/slack.ts checks them.
export const getSlack = (pid: string) => httpGet(`/projects/${encodeURIComponent(pid)}/slack`).then((r) => r.body);
/** 204: resolves with the HTTP status. */
export const deleteSlack = (pid: string) => httpDelete(`/projects/${encodeURIComponent(pid)}/slack`).then((r) => r.status);

/** 200 `{posted, channel}` (docs/mission-control/integrations-api.md "Slack test message"): the body stays `unknown`. */
export const testSlackConnection = (pid: string) => httpPost(`/projects/${encodeURIComponent(pid)}/slack/test`).then((r) => r.body);

// The project's integrations (docs/mission-control/integrations-api.md): who each app is connected by. Bodies stay `unknown`; the
// integrations port (lib/data/http/integrations.ts) checks them.
export const listProjectIntegrations = (pid: string) => httpGet(`/projects/${encodeURIComponent(pid)}/integrations`).then((r) => r.body);
export const putProjectIntegration = (pid: string, app: string) =>
  httpPut(`/projects/${encodeURIComponent(pid)}/integrations/${encodeURIComponent(app)}`).then((r) => r.body);
/** 204: resolves with the HTTP status. */
export const deleteProjectIntegration = (pid: string, app: string) =>
  httpDelete(`/projects/${encodeURIComponent(pid)}/integrations/${encodeURIComponent(app)}`).then((r) => r.status);

// Jira settings of a project (docs/jira-sync/api.md#web-routes). Bodies stay `unknown`; the Jira port (lib/data/http/jira.ts) checks
// their shape. A failed access check is a 200 answer.
export const getJira = (pid: string) => httpGet(`/projects/${encodeURIComponent(pid)}/jira`).then((r) => r.body);
export const putJira = (pid: string, jiraProject: string, epicKey: string | null, enabled: boolean) =>
  httpPut(`/projects/${encodeURIComponent(pid)}/jira`, json({ jira_project: jiraProject, epic_key: epicKey, enabled })).then((r) => r.body);
export const checkJiraAccess = (pid: string, jiraProject: string, epicKey: string | null) =>
  httpPost(`/projects/${encodeURIComponent(pid)}/jira/access-check`, json({ jira_project: jiraProject, epic_key: epicKey })).then(
    (r) => r.body,
  );

// The Jira gap channel of a project (docs/jira-sync/gaps.md). Bodies stay `unknown`; lib/data/http/jira-gaps.ts checks them.
export const getJiraGaps = (pid: string) => httpGet(`/projects/${encodeURIComponent(pid)}/jira-gaps`).then((r) => r.body);
export const putJiraGaps = (pid: string, jiraProject: string, grouping: string, enabled: boolean) =>
  httpPut(`/projects/${encodeURIComponent(pid)}/jira-gaps`, json({ jira_project: jiraProject, grouping, enabled })).then((r) => r.body);
export const checkJiraGapsAccess = (pid: string, jiraProject: string) =>
  httpPost(`/projects/${encodeURIComponent(pid)}/jira-gaps/access-check`, json({ jira_project: jiraProject })).then((r) => r.body);

// The Linear gap channel of a project (docs/linear/gaps.md). Bodies stay `unknown`; lib/data/http/linear-gaps.ts checks them.
export const getLinearGaps = (pid: string) => httpGet(`/projects/${encodeURIComponent(pid)}/linear-gaps`).then((r) => r.body);
export const putLinearGaps = (pid: string, teamKey: string, linearProject: string | null, grouping: string, enabled: boolean) =>
  httpPut(
    `/projects/${encodeURIComponent(pid)}/linear-gaps`,
    json({ team_key: teamKey, linear_project: linearProject, grouping, enabled }),
  ).then((r) => r.body);
export const checkLinearGapsAccess = (pid: string, teamKey: string, linearProject: string | null) =>
  httpPost(
    `/projects/${encodeURIComponent(pid)}/linear-gaps/access-check`,
    json({ team_key: teamKey, linear_project: linearProject }),
  ).then((r) => r.body);

// The GitHub Projects plan destination of a project (docs/github/projects.md). Bodies stay `unknown`; lib/data/http/github-projects.ts checks them.
/** The project is named by `nodeId` (an existing one) or `newTitle` (created in the organization at save): the body names exactly one. */
const projectBody = (pick: { nodeId: string } | { newTitle: string }) =>
  "nodeId" in pick ? { project_node_id: pick.nodeId } : { new_project_title: pick.newTitle };
export const getGitHubProjects = (pid: string) => httpGet(`/projects/${encodeURIComponent(pid)}/github-projects`).then((r) => r.body);
export const listGitHubProjects = (pid: string, repositoryId: string) =>
  httpGet(`/projects/${encodeURIComponent(pid)}/github-projects/projects?repository_id=${encodeURIComponent(repositoryId)}`).then(
    (r) => r.body,
  );
export const putGitHubProjects = (pid: string, repositoryId: string, pick: { nodeId: string } | { newTitle: string }, enabled: boolean) =>
  httpPut(
    `/projects/${encodeURIComponent(pid)}/github-projects`,
    json({ repository_id: repositoryId, ...projectBody(pick), enabled }),
  ).then((r) => r.body);
export const checkGitHubProjectsAccess = (pid: string, repositoryId: string, pick: { nodeId: string } | { newTitle: string }) =>
  httpPost(
    `/projects/${encodeURIComponent(pid)}/github-projects/access-check`,
    json({ repository_id: repositoryId, ...projectBody(pick) }),
  ).then((r) => r.body);

// The caller's Atlassian connection (docs/jira-sync/oauth.md#routes). The consent callback is not here: the route handler
// app/api/integrations/atlassian/callback calls the API on the server.
export const getAtlassianConnection = () => httpGet("/integrations/atlassian").then((r) => r.body);
export const authorizeAtlassian = (returnTo: string) =>
  httpPost("/integrations/atlassian/authorize", json({ return_to: returnTo })).then((r) => r.body);
/** 204: resolves with the HTTP status. */
export const disconnectAtlassian = () => httpDelete("/integrations/atlassian").then((r) => r.status);

// Linear (plans/LINEAR, stream S1): the caller's connection. Bodies stay `unknown`; the Linear port (lib/data/http/linear.ts) checks their shape. The
// OAuth callback is not here: the route handler app/api/integrations/linear/callback calls the API on the server.
export const getLinearConnection = () => httpGet("/integrations/linear").then((r) => r.body);
export const authorizeLinear = (returnTo: string) =>
  httpPost("/integrations/linear/authorize", json({ return_to: returnTo })).then((r) => r.body);
/** 204: resolves with the HTTP status. */
export const disconnectLinear = () => httpDelete("/integrations/linear").then((r) => r.status);

// GitHub (docs/github/api.md): the caller's connection, the repositories it can read and the repositories of a
// project. Bodies stay `unknown`; the GitHub port (lib/data/http/github.ts) checks their shape. The OAuth callback and the setup return
// are not here: the route handlers app/api/integrations/github/{callback,setup} call the API on the server.
export const getGitHubConnection = () => httpGet("/integrations/github").then((r) => r.body);
export const authorizeGitHub = (returnTo: string) =>
  httpPost("/integrations/github/authorize", json({ return_to: returnTo })).then((r) => r.body);
export const installGitHub = (returnTo: string) =>
  httpPost("/integrations/github/install", json({ return_to: returnTo })).then((r) => r.body);
/** 204: resolves with the HTTP status. */
export const disconnectGitHub = () => httpDelete("/integrations/github").then((r) => r.status);
export const listGitHubRepositories = () => httpGet("/integrations/github/repositories").then((r) => r.body);
export const listProjectRepositories = (pid: string) => httpGet(`/projects/${encodeURIComponent(pid)}/repositories`).then((r) => r.body);
export const addProjectRepository = (pid: string, githubRepoId: number) =>
  httpPost(`/projects/${encodeURIComponent(pid)}/repositories`, json({ github_repo_id: githubRepoId })).then((r) => r.body);
/** 204: resolves with the HTTP status. */
export const removeProjectRepository = (pid: string, rid: string) =>
  httpDelete(`/projects/${encodeURIComponent(pid)}/repositories/${encodeURIComponent(rid)}`).then((r) => r.status);
export const cloneProjectRepository = (pid: string, rid: string) =>
  httpPost(`/projects/${encodeURIComponent(pid)}/repositories/${encodeURIComponent(rid)}/clone`).then((r) => r.body);

// Sources (GDRIVE plan 04, architecture-notes B4). Bodies are returned as `unknown`: the http adapter (lib/data/http/sources.ts)
// checks their shape. POSTs go through `call`, so they carry the CSRF header.
export const getDriveIdentity = () => httpGet("/drive/identity").then((r) => r.body);
// sources-ux (INT-SRC-0001..0004): `?batches=1` asks for the v2 status; the others are the file list, removal and the Local files batch.
export const getSourcesStatus = (pid: string) => httpGet(`/projects/${encodeURIComponent(pid)}/sources?batches=1`).then((r) => r.body);
export const listSourceFiles = (pid: string, sid: string, cursor?: string | null) => {
  const next = cursor === undefined || cursor === null || cursor === "" ? "" : `&cursor=${encodeURIComponent(cursor)}`;
  return httpGet(`/projects/${encodeURIComponent(pid)}/sources/${encodeURIComponent(sid)}/files?limit=${String(PAGE_LIMIT)}${next}`).then(
    (r) => r.body,
  );
};
/** DELETE goes through `call`, so it carries the CSRF header like every non-GET. Resolves with the HTTP status (204 = removed). */
export const removeSource = (pid: string, sid: string) =>
  httpDelete(`/projects/${encodeURIComponent(pid)}/sources/${encodeURIComponent(sid)}`).then((r) => r.status);
/** `name` (optional, the batch's own name) is left out of the body when blank, so the server keeps the default. */
export const startBatch = (pid: string, expectedFiles: number, name?: string) =>
  httpPost(
    `/projects/${encodeURIComponent(pid)}/sources/uploads`,
    json(name === undefined || name.trim() === "" ? { expected_files: expectedFiles } : { expected_files: expectedFiles, name }),
  ).then((r) => r.body);
/** Renames an upload batch: 200 with the source summary, 422 for a bad name, 404 `source_not_found`, 409 `source_not_renamable`. */
export const renameSource = (pid: string, sid: string, name: string) =>
  httpPatch(`/projects/${encodeURIComponent(pid)}/sources/${encodeURIComponent(sid)}`, json({ name })).then((r) => r.body);
/** Same wire form as `uploadDocument`: the File is the raw body; the `Content-Type` is the canonical type of its extension. */
export const addBatchFile = (pid: string, sid: string, file: File) =>
  httpPost(`/projects/${encodeURIComponent(pid)}/sources/${encodeURIComponent(sid)}/files?filename=${encodeURIComponent(file.name)}`, {
    headers: { "Content-Type": contentTypeFor(file.name) },
    body: file,
  }).then((r) => r.body);
export const endBatch = (pid: string, sid: string) =>
  httpPost(`/projects/${encodeURIComponent(pid)}/sources/${encodeURIComponent(sid)}/end`).then((r) => r.body);
export const testDriveFolder = (pid: string, link: string) =>
  httpPost(`/projects/${encodeURIComponent(pid)}/sources/gdrive/test`, json({ link })).then((r) => r.body);
export const saveDriveFolder = (pid: string, link: string, exclude: readonly string[] = []) =>
  httpPost(`/projects/${encodeURIComponent(pid)}/sources/gdrive`, json(exclude.length === 0 ? { link } : { link, exclude })).then(
    (r) => r.body,
  );
export const checkConfluenceLink = (pid: string, link: string, follow: boolean) =>
  httpPost(`/projects/${encodeURIComponent(pid)}/sources/confluence/check`, json({ link, follow })).then((r) => r.body);
export const addConfluenceSource = (pid: string, link: string, follow: boolean) =>
  httpPost(`/projects/${encodeURIComponent(pid)}/sources/confluence`, json({ link, follow })).then((r) => r.body);
export const checkJiraEpicLink = (pid: string, keyOrLink: string) =>
  httpPost(`/projects/${encodeURIComponent(pid)}/sources/jira-epic/check`, json({ key_or_link: keyOrLink })).then((r) => r.body);
export const addJiraEpicSource = (pid: string, keyOrLink: string, exclude: readonly string[] = []) =>
  httpPost(
    `/projects/${encodeURIComponent(pid)}/sources/jira-epic`,
    json(exclude.length === 0 ? { key_or_link: keyOrLink } : { key_or_link: keyOrLink, exclude }),
  ).then((r) => r.body);
export const checkLinearSourceLink = (pid: string, link: string) =>
  httpPost(`/projects/${encodeURIComponent(pid)}/sources/linear/check`, json({ link })).then((r) => r.body);
export const addLinearSourceLink = (pid: string, link: string, exclude: readonly string[] = []) =>
  httpPost(`/projects/${encodeURIComponent(pid)}/sources/linear`, json(exclude.length === 0 ? { link } : { link, exclude })).then(
    (r) => r.body,
  );
// Knowledge build (`/graph_build`): the GET is the build state, the POST starts a build. Bodies stay `unknown`; the knowledge port
// (lib/data/http/knowledge.ts) checks their shape. The reasoning level is an integer 1..5 (the backend only logs it).
export const getGraphBuild = (pid: string) => httpGet(`/projects/${encodeURIComponent(pid)}/graph_build`).then((r) => r.body);
export const startGraphBuild = (pid: string, reasoningLevel: number) =>
  httpPost(`/projects/${encodeURIComponent(pid)}/graph_build`, json({ reasoning_level: reasoningLevel })).then((r) => r.body);
// Plan (`/plan`, `graph.plan`): the GET is the state of the latest run, the POST starts a run (`replan` re-plans every area), `/plan/tasks` is
// the persisted plan and `/plan/preview` the features to plan with the run estimate. Bodies stay `unknown`; the plan port
// (lib/data/http/plan.ts) checks their shape.
export const getPlan = (pid: string) => httpGet(`/projects/${encodeURIComponent(pid)}/plan`).then((r) => r.body);
export const startPlan = (pid: string, replan: boolean) =>
  httpPost(`/projects/${encodeURIComponent(pid)}/plan`, json({ replan })).then((r) => r.body);
export const getPlanTasks = (pid: string) => httpGet(`/projects/${encodeURIComponent(pid)}/plan/tasks`).then((r) => r.body);
export const getPlanPreview = (pid: string) => httpGet(`/projects/${encodeURIComponent(pid)}/plan/preview`).then((r) => r.body);

/** `created` = a new document (201). `duplicate` = the project already has this content and nothing new happened; `retried` =
 * it had it in status `failed`, and the server started its parse again (both 200, with the existing document). */
export type UploadOutcome = "created" | "duplicate" | "retried";

/** AC-12.2: the File is the raw body, `Content-Type` = the canonical type of its extension (not `file.type`); no multipart. */
export async function uploadDocument(pid: string, file: File): Promise<{ outcome: UploadOutcome; document: DocumentSummary }> {
  const { status, body: raw } = await httpPost(`/projects/${encodeURIComponent(pid)}/documents?filename=${encodeURIComponent(file.name)}`, {
    headers: { "Content-Type": contentTypeFor(file.name) },
    body: file,
  });
  const body = raw as { outcome?: string; document: DocumentSummary };
  let outcome: UploadOutcome = "created";
  if (body.outcome === "retried") outcome = "retried";
  else if (status === 200 || body.outcome === "duplicate") outcome = "duplicate";
  return { outcome, document: body.document };
}

// Findings and claims of the Plan page (`/findings`, `/plan/claims`): people resolve or dismiss findings and force-release a story
// claim. Bodies stay `unknown`; the work port (lib/data/http/work.ts) checks their shape.
export const getFindings = (pid: string, status: string, node: string | undefined) => {
  const query = new URLSearchParams({ limit: "200", status });
  if (node !== undefined) query.set("node", node);
  return httpGet(`/projects/${encodeURIComponent(pid)}/findings?${query.toString()}`).then((r) => r.body);
};
export const resolveFinding = (pid: string, fid: string, note: string | undefined) =>
  httpPost(`/projects/${encodeURIComponent(pid)}/findings/${encodeURIComponent(fid)}/resolve`, json({ note: note ?? null })).then(
    (r) => r.body,
  );
export const dismissFinding = (pid: string, fid: string, note: string | undefined) =>
  httpPost(`/projects/${encodeURIComponent(pid)}/findings/${encodeURIComponent(fid)}/dismiss`, json({ note: note ?? null })).then(
    (r) => r.body,
  );
// The Gaps screen (FR-GWEB-04, FR-GWEB-06): the gap list (`query` is the encoded `list_gaps` filters, never empty: the screens always
// send a status; a list filter repeating its parameter), one gap, the latest gap run with its noise report, and the writes: edits
// preview, answer, dismiss, assign, raise, apply now, the "Answered by AI" list and revert, and the noise rules. Bodies stay `unknown`;
// the gaps port (lib/data/http/gaps.ts) checks their shape. POSTs, PUTs and DELETEs go through `call`, so they carry the CSRF header
// (the BFF adds nothing; the browser sends the Origin).
export const getGaps = (pid: string, query: string) => httpGet(`/projects/${encodeURIComponent(pid)}/gaps?${query}`).then((r) => r.body);
export const getGap = (pid: string, gid: string) =>
  httpGet(`/projects/${encodeURIComponent(pid)}/gaps/${encodeURIComponent(gid)}`).then((r) => r.body);
export const getLatestGapRun = (pid: string) => httpGet(`/projects/${encodeURIComponent(pid)}/gap/runs/latest`).then((r) => r.body);
export const previewGapEdits = (pid: string, gid: string, body: Record<string, unknown>) =>
  httpPost(`/projects/${encodeURIComponent(pid)}/gaps/${encodeURIComponent(gid)}/edits_preview`, json(body)).then((r) => r.body);
export const answerGap = (pid: string, gid: string, body: Record<string, unknown>) =>
  httpPost(`/projects/${encodeURIComponent(pid)}/gaps/${encodeURIComponent(gid)}/answer`, json(body)).then((r) => r.body);
/** Resolves with the HTTP status (204 = dismissed). */
export const dismissGap = (pid: string, gid: string, reason: string) =>
  httpPost(`/projects/${encodeURIComponent(pid)}/gaps/${encodeURIComponent(gid)}/dismiss`, json({ reason })).then((r) => r.status);
export const raiseGap = (pid: string, body: Record<string, unknown>) =>
  httpPost(`/projects/${encodeURIComponent(pid)}/gaps`, json(body)).then((r) => r.body);
export const applyGapAnswers = (pid: string) => httpPost(`/projects/${encodeURIComponent(pid)}/gaps/apply`).then((r) => r.body);
export const getAiAnswers = (pid: string) => httpGet(`/projects/${encodeURIComponent(pid)}/gaps/ai_answers?limit=50`).then((r) => r.body);
export const revertClarification = (pid: string, clr: string, reason: string) =>
  httpPost(`/projects/${encodeURIComponent(pid)}/gaps/clarifications/${encodeURIComponent(clr)}/revert`, json({ reason })).then(
    (r) => r.body,
  );
export const getNoiseRules = (pid: string) => httpGet(`/projects/${encodeURIComponent(pid)}/gaps/noise_rules`).then((r) => r.body);
export const addNoiseRule = (pid: string, body: Record<string, unknown>) =>
  httpPost(`/projects/${encodeURIComponent(pid)}/gaps/noise_rules`, json(body)).then((r) => r.body);
/** Resolves with the HTTP status (204 = removed). */
export const removeNoiseRule = (pid: string, rid: string) =>
  httpDelete(`/projects/${encodeURIComponent(pid)}/gaps/noise_rules/${encodeURIComponent(rid)}`).then((r) => r.status);
/** Assigns a gap to a member, or clears it (`userId` null); resolves with the gap. */
export const assignGap = (pid: string, gid: string, userId: string | null) =>
  httpPut(`/projects/${encodeURIComponent(pid)}/gaps/${encodeURIComponent(gid)}/assignee`, json({ user_id: userId })).then((r) => r.body);
export const getClaims = (pid: string) => httpGet(`/projects/${encodeURIComponent(pid)}/plan/claims`).then((r) => r.body);
export const releaseClaim = (pid: string, sid: string) =>
  httpPost(`/projects/${encodeURIComponent(pid)}/plan/claims/${encodeURIComponent(sid)}/release`).then((r) => r.body);
