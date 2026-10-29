// FE-PPL-01.AC3, FE-BP-02.AC8 (TECH-SPECS §3.3, DECISIONS.md G01-10): a small same-origin GET helper for `GET /me`.
// `lib/api.ts` has no `/me` function and stays unchanged, so this helper repeats its rules: same origin, `credentials:
// "same-origin"`, `X-Requested-With: specflow`, the error envelope parsed the same way (never raw HTML or server text), the
// `ApiError` class of `lib/api.ts`, and `UNAUTHENTICATED_EVENT` on 401. GET only: the BFF's CSRF rule (Origin) is for mutations.
import { ApiError, getBackend, readEnvelope } from "@/lib/api";
import { DataError } from "@/lib/domain/errors";

/** GET `/api/backend<path>` and parse JSON; throws `ApiError` like `lib/api.ts` (`api.ts:40-68`). */
export async function getJson<T>(path: string): Promise<T> {
  let res: Response;
  try {
    res = await getBackend(path);
  } catch (e) {
    if (e instanceof Error && e.name === "AbortError") throw new DataError("aborted", false);
    throw new DataError("network", true); // the fetch call itself rejected
  }
  const { body, parsed } = await readEnvelope(res);
  if (!res.ok) {
    const code = parsed && typeof body?.error?.code === "string" ? body.error.code : `http_${String(res.status)}`;
    throw new ApiError(res.status, code);
  }
  if (!parsed || body === null) throw new ApiError(res.status, "invalid_response");
  return body as T;
}
