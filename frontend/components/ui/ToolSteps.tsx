// FE-SPEC-02, FE-DS-02 (R5): port of `ToolSteps` in bundle.js: what SpecBot did before answering. A running step shows a
// Spinner, a done step its icon (default check).
import { Icon, type IconName } from "./Icon";
import { Spinner } from "./ProgressBar";
import { truthy } from "./utils";

export interface ToolStep {
  label: string;
  detail?: string;
  status?: "running" | "done";
  icon?: IconName;
}

export function ToolSteps({ steps }: Readonly<{ steps: ToolStep[] }>) {
  return (
    <ol className="gdg-steps" data-testid="tool-steps">
      {steps.map((s, i) => (
        <li key={i} className={`is-${s.status ?? "done"}`}>
          {s.status === "running" ? <Spinner size={14} /> : <Icon name={s.icon ?? "check"} size={14} />}
          <span>{s.label}</span>
          {truthy(s.detail) ? <code>{s.detail}</code> : null}
        </li>
      ))}
    </ol>
  );
}
