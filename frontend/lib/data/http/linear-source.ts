// plans/LINEAR/LINEAR-PLAN.md, stream S4: the production `LinearSourcePort` over `/projects/{pid}/sources/linear`. FOUNDATION STUB: it refuses
// everything until S4 calls the API.
import { DataError } from "@/lib/domain/errors";
import type { LinearSourcePort } from "@/lib/domain/linear-source";

const notAvailable = (): Promise<never> => Promise.reject(new DataError("not_available", false));

export function createHttpLinearSource(): LinearSourcePort {
  return { check: notAvailable, add: notAvailable };
}
