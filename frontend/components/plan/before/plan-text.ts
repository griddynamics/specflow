// FE-PLAN-02.AC5 (MOCKS §15.8): the exact Stage 0 strings, kept pure, apart from React.
export const CREATE_PLAN_LABEL = "Create PLAN";
/** MOCKS §15.8 `recreatePlan`: the glass button right of the Plan | Develop switch (Stage 2 only, FE-PLAN-13). */
export const RECREATE_PLAN_LABEL = "Recreate Plan";
/** The empty state of Stage 0 without features (no finished build, or a graph without features). */
export const NO_FEATURES_TITLE = "No features to plan yet";
export const NO_FEATURES_BODY = "The plan is made from the knowledge graph. Build it in Mission Control first.";

const COUNT = new Intl.NumberFormat("en");

/** The line above the table: how many features, and the model calls and input tokens the run is estimated to need. */
export function previewSummary(features: number, calls: number | null, inputTokens: number | null): string {
  const lead = `${COUNT.format(features)} ${features === 1 ? "feature" : "features"} to plan`;
  if (calls === null || inputTokens === null) return `${lead}.`;
  const callWord = calls === 1 ? "model call" : "model calls";
  return `${lead}. Estimated ${COUNT.format(calls)} ${callWord}, about ${COUNT.format(inputTokens)} input tokens.`;
}

/** The note shown when the last plan run failed before a plan existed. */
export function failureNote(code: string): string {
  return `The last plan run failed (${code}). Create the plan again to retry.`;
}
