// plans/LINEAR/LINEAR-PLAN.md, stream S1: the production `LinearPort`. FOUNDATION STUB: it reports "off" and refuses everything until S1 calls
// the API. Failures are `DataError`s whose `detail.apiCode` is the API code.
import { DataError } from "@/lib/domain/errors";
import type { LinearPort } from "@/lib/domain/linear";

const notAvailable = (): Promise<never> => Promise.reject(new DataError("not_available", false));

export function createHttpLinear(): LinearPort {
  return {
    connection: () => Promise.resolve({ available: false, status: "none", userName: null, workspace: null }),
    authorize: notAvailable,
    disconnect: notAvailable,
  };
}
