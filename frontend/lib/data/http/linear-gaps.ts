// plans/LINEAR/LINEAR-PLAN.md, stream S5: the production `LinearGapsPort` over `/projects/{pid}/linear-gaps`. FOUNDATION STUB: it reports the
// channel as off until S5 calls the API.
import type { LinearGapsPort } from "@/lib/domain/linear-gaps";

export function createHttpLinearGaps(): LinearGapsPort {
  return {
    get: () =>
      Promise.resolve({
        available: false,
        enabled: false,
        teamKey: null,
        grouping: null,
        parentIdentifier: null,
        parentUrl: null,
        posted: 0,
        answered: 0,
        errorCode: null,
      }),
  };
}
