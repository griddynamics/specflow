// FE-DATA-01, FE-BP-02, FE-BP-08 (TECH-SPECS §2.13, §3.3): every failure of the http adapter becomes a `DataError`.
// `lib/api.ts` throws `ApiError(status, code)`: `code` is the BFF/API envelope code (`{ error: { code } }`) or `http_<status>` /
// `invalid_response` when the body was not the envelope (`lib/api.ts:51-68`). Mapping: by `code` first, then by `status`.
// Ports never pass server text on: a DataError carries a code, the HTTP status and the API code only.
import { ApiError } from "@/lib/api";
import { DataError, isDataError, type DataErrorCode } from "@/lib/domain/errors";

/** API / BFF error codes with a fixed meaning (TECH-SPECS §2.13 table). */
const BY_CODE: Record<string, DataErrorCode> = {
  unauthenticated: "unauthenticated",
  project_not_found: "not_found",
  document_not_found: "not_found",
  not_found: "not_found",
  already_parsed: "conflict",
  document_not_parsed: "conflict",
  validation_failed: "validation",
  model_not_allowed: "validation",
  missing_filename: "validation",
  invalid_path: "validation",
  unsupported_format: "unsupported_format",
  empty_file: "empty_file",
  upstream_unavailable: "unavailable",
  workflow_start_failed: "unavailable",
  database_unavailable: "unavailable",
  source_not_found: "not_found", // sources-ux: the detail's apiCode tells it from a plain `not_found` (an old API's unknown route)
  batch_ended: "conflict",
  removal_unavailable: "unavailable",
  invalid_response: "invalid_response",
  csrf_rejected: "unknown", // a bug in the client, not a user error
};

/** Retryable codes (TECH-SPECS §2.13): the same request may succeed later. */
const RETRYABLE: ReadonlySet<DataErrorCode> = new Set<DataErrorCode>(["unavailable", "network", "invalid_response"]);

/** HTTP status -> code when the API code is not a known one. */
export function codeOfStatus(status: number): DataErrorCode {
  if (status === 401) return "unauthenticated";
  if (status === 404) return "not_found";
  if (status === 409) return "conflict";
  if (status === 415) return "unsupported_format";
  if (status === 400 || status === 422) return "validation";
  if (status >= 500) return "unavailable";
  return "unknown"; // 403 csrf_rejected and anything else
}

export const isRetryableCode = (code: DataErrorCode): boolean => RETRYABLE.has(code);

const make = (code: DataErrorCode, detail?: { status?: number; apiCode?: string }) => new DataError(code, isRetryableCode(code), detail);

/** FE-DATA-01 (TECH-SPECS §2.13): any thrown value -> `DataError`. A `DataError` passes through unchanged. */
export function toDataError(e: unknown): DataError {
  if (isDataError(e)) return e;
  if (e instanceof ApiError) {
    const code = Object.hasOwn(BY_CODE, e.code) ? BY_CODE[e.code] : codeOfStatus(e.status);
    return make(code, { status: e.status, apiCode: e.code });
  }
  if (e instanceof Error && e.name === "AbortError") return make("aborted");
  // A TypeError is `network` only where the caller knows it came from the fetch call itself (`guard`, `client.ts`); anywhere
  // else it is a bug or a malformed answer, never "Service unavailable".
  return make("unknown");
}

/** The answer had the wrong shape (a 2xx body that is not the contract). Retryable, like `lib/api.ts`'s `invalid_response`. */
export const invalidResponse = () => new DataError("invalid_response", true);

/** Throws `invalid_response` unless `ok`. */
export function expectShape(ok: unknown): asserts ok {
  const holds = Boolean(ok);
  if (!holds) throw invalidResponse();
}

export const isRecord = (v: unknown): v is Record<string, unknown> => typeof v === "object" && v !== null && !Array.isArray(v);
export const isString = (v: unknown): v is string => typeof v === "string";

/**
 * Calls one `lib/api.ts` function and rejects only with a `DataError` (TECH-SPECS §2.1). A `TypeError` thrown by that call is
 * its `fetch` rejecting (offline, DNS, reset) -> `network`; the response shape is checked by the caller AFTER this call with
 * `expectShape`, so a malformed body is `invalid_response`, never `network`.
 */
export async function guard<T>(fn: () => Promise<T>): Promise<T> {
  try {
    return await fn();
  } catch (e) {
    if (e instanceof TypeError) throw make("network");
    throw toDataError(e);
  }
}

/** Shape-checks a value inside a port method: any exception becomes a `DataError` (`invalid_response` for shape errors). */
export function checked<T>(fn: () => T): T {
  try {
    return fn();
  } catch (e) {
    if (isDataError(e)) throw e;
    throw invalidResponse();
  }
}
