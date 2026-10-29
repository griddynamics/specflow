// Browser-side client for the BFF (/api/backend/* -> API /api/v1/*). Every call is
// same-origin: the session cookie is HttpOnly and tokens never reach this code.

import { PAGE_LIMIT } from "@/lib/domain/source-state";

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

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string = code,
  ) {
    super(message);
  }
}

export const UNAUTHENTICATED_EVENT = "specflow:unauthenticated";
const BASE = "/api/backend";
const HEADERS = { "X-Requested-With": "specflow" };

export interface ParsedEnvelope {
  body: { error?: { code?: unknown } } | null;
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

/** A same-origin GET of `/api/backend<path>`: the raw response, with the CSRF header and the session cookie. */
export const getBackend = (path: string): Promise<Response> =>
  fetch(`${BASE}${path}`, { method: "GET", credentials: "same-origin", headers: HEADERS });

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
    if (parsed && code === body?.error?.code) throw new ApiError(res.status, code);
    throw new ApiError(res.status, code, `Request failed (HTTP ${String(res.status)}). Please try again.`);
  }
  if (!parsed) throw new ApiError(res.status, "invalid_response", `Unexpected response from the server (HTTP ${String(res.status)}).`);
  return { status: res.status, body };
}

// One helper per HTTP method, so every call site reads `METHOD path` and maps to exactly one endpoint of the API contract
// (backend/contracts/openapi.json, checked against frontend/validation/api-contracts.json). Paths are written in full at the call.
const httpGet = (path: string) => call(path);
const httpPost = (path: string, init: RequestInit = {}) => call(path, { ...init, method: "POST" });
const httpDelete = (path: string) => call(path, { method: "DELETE" });

const json = (value: unknown): RequestInit => ({
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify(value),
});

export const listProjects = () => httpGet("/projects?limit=100").then((r) => r.body as Page<Project>);
export const createProject = (name: string) => httpPost("/projects", json({ name })).then((r) => r.body as Project);
export const getProject = (pid: string) => httpGet(`/projects/${encodeURIComponent(pid)}`).then((r) => r.body as Project);

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
export const startBatch = (pid: string, expectedFiles: number) =>
  httpPost(`/projects/${encodeURIComponent(pid)}/sources/uploads`, json({ expected_files: expectedFiles })).then((r) => r.body);
/** Same wire form as `uploadDocument`: the File is the raw body, `Content-Type: application/pdf`. */
export const addBatchFile = (pid: string, sid: string, file: File) =>
  httpPost(`/projects/${encodeURIComponent(pid)}/sources/${encodeURIComponent(sid)}/files?filename=${encodeURIComponent(file.name)}`, {
    headers: { "Content-Type": "application/pdf" },
    body: file,
  }).then((r) => r.body);
export const endBatch = (pid: string, sid: string) =>
  httpPost(`/projects/${encodeURIComponent(pid)}/sources/${encodeURIComponent(sid)}/end`).then((r) => r.body);
export const testDriveFolder = (pid: string, link: string) =>
  httpPost(`/projects/${encodeURIComponent(pid)}/sources/gdrive/test`, json({ link })).then((r) => r.body);
export const saveDriveFolder = (pid: string, link: string) =>
  httpPost(`/projects/${encodeURIComponent(pid)}/sources/gdrive`, json({ link })).then((r) => r.body);

/** `created` = a new document (201). `duplicate` = the project already has this content and nothing new happened; `retried` =
 * it had it in status `failed`, and the server started its parse again (both 200, with the existing document). */
export type UploadOutcome = "created" | "duplicate" | "retried";

/** AC-12.2: the File is the raw body, `Content-Type: application/pdf`; no multipart. */
export async function uploadDocument(pid: string, file: File): Promise<{ outcome: UploadOutcome; document: DocumentSummary }> {
  const { status, body: raw } = await httpPost(`/projects/${encodeURIComponent(pid)}/documents?filename=${encodeURIComponent(file.name)}`, {
    headers: { "Content-Type": "application/pdf" },
    body: file,
  });
  const body = raw as { outcome?: string; document: DocumentSummary };
  let outcome: UploadOutcome = "created";
  if (body.outcome === "retried") outcome = "retried";
  else if (status === 200 || body.outcome === "duplicate") outcome = "duplicate";
  return { outcome, document: body.document };
}
