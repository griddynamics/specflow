// plans/LINEAR/LINEAR-PLAN.md, stream S3: the production `LinearPlanPort` over `/projects/{pid}/linear-plan`. FOUNDATION STUB: it reports the
// destination as off until S3 calls the API.
import type { LinearPlanPort } from "@/lib/domain/linear-plan";

export function createHttpLinearPlan(): LinearPlanPort {
  return {
    get: () =>
      Promise.resolve({
        available: false,
        enabled: false,
        teamId: null,
        teamKey: null,
        linearProjectId: null,
        linearProjectName: null,
        linearProjectUrl: null,
        sync: null,
      }),
  };
}
