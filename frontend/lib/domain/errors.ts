// FE-DATA-01: one failure shape for every port (TECH-SPECS §2.13).
export type DataErrorCode =
  | "unauthenticated"
  | "not_found"
  | "conflict"
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

export class DataError extends Error {
  readonly code: DataErrorCode;
  readonly retryable: boolean;
  readonly detail?: { status?: number; apiCode?: string; maxBytes?: number };

  constructor(code: DataErrorCode, retryable: boolean, detail?: { status?: number; apiCode?: string; maxBytes?: number }) {
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
