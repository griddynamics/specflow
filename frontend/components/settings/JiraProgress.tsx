// docs/jira-sync/ui.md#progress: the progress of a running Jira sync pass: the step, "done of total" and a bar. Shared by the Jira tab and
// the Plan. The bar is the design system's progress track (`gdg-progress`); `done` is clamped by `progressView` (the total can grow while
// a step runs), and a total of 0 (nothing to do yet) shows the step text alone, with no count and no bar.
import type { JiraSyncProgress } from "@/lib/domain/jira";
import { progressView } from "./jira-logic";

export function JiraProgress({ progress, testId = "jira-progress" }: Readonly<{ progress: JiraSyncProgress; testId?: string }>) {
  const v = progressView(progress);
  if (v.total === 0) {
    return (
      <div className="gdg-progress" data-testid={testId} data-step={progress.step}>
        <div className="gdg-progress__head">
          <span>{v.step}</span>
        </div>
      </div>
    );
  }
  return (
    <div className="gdg-progress" data-testid={testId} data-step={progress.step}>
      <div className="gdg-progress__head">
        <span>{v.step}</span>
        <span>{v.count}</span>
      </div>
      <div
        className="gdg-progress__track"
        role="progressbar"
        aria-label={v.step}
        aria-valuemin={0}
        aria-valuemax={v.total}
        aria-valuenow={v.done}
        aria-valuetext={v.count}
      >
        <div className="gdg-progress__fill" style={{ width: `${String((v.done / v.total) * 100)}%` }} />
      </div>
    </div>
  );
}
