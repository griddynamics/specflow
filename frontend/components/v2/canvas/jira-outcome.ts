// What a failed Jira call leaves on a canvas form (the plan destination and the gap sub-tasks share it): the texts under the fields the API named,
// or one alert line.
import { failureOf, type Outcome } from "@/components/shared/settings/jira-logic";

export function problemOf(e: unknown): Outcome {
  const f = failureOf(e);
  return {
    kind: "problem",
    texts: f.kind === "fields" ? f.texts : { alert: f.kind === "toast" || f.kind === "disconnected" ? f.text : "" },
  };
}
