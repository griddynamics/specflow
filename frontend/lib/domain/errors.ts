// FE-DATA-01: one failure shape for every port (TECH-SPECS §2.13).
export type DataErrorCode =
  | "unauthenticated"
  | "not_found"
  | "conflict"
  | "forbidden"
  | "validation"
  | "unsupported_format"
  | "file_too_large"
  | "empty_file"
  | "unavailable"
  | "network"
  | "invalid_response"
  | "not_available"
  | "aborted"
  | "unknown";

/** One entry of a 422 `details.errors` (api.md#errors): `loc` is the body path, `type` the rule that failed. */
export interface ErrorEntry {
  loc: (string | number)[];
  type: string;
}

/** What a failure may carry besides its code: the HTTP status, the API code and, from the envelope's `details`, the cap of a 413, the
 * `field` and `reason` of a 400 `answer_invalid` and the gap's current `state` of a 409 `gap_wrong_state` (never server text). */
export interface DataErrorDetail {
  status?: number;
  apiCode?: string;
  maxBytes?: number;
  field?: string;
  reason?: string;
  /** The gap's status now (409 `gap_wrong_state`). */
  state?: "open" | "answered" | "applying" | "resolved";
  errors?: ErrorEntry[];
}

export class DataError extends Error {
  readonly code: DataErrorCode;
  readonly retryable: boolean;
  readonly detail?: DataErrorDetail;

  constructor(code: DataErrorCode, retryable: boolean, detail?: DataErrorDetail) {
    super(code); // a code, never server text (TECH-SPECS §2.1)
    this.name = "DataError";
    this.code = code;
    this.retryable = retryable;
    this.detail = detail;
  }
}

export function isDataError(e: unknown): e is DataError {
  return e instanceof DataError;
}
