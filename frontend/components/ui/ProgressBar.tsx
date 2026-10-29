// FE-DS-02 (R5): port of `ProgressBar` and `Spinner` in bundle.js.
import { cx, nonEmpty, truthy } from "./utils";

export interface ProgressBarProps {
  value?: number;
  label?: string;
  indeterminate?: boolean;
  tone?: "danger";
}

export interface SpinnerProps {
  size?: number;
  label?: string;
}

export function ProgressBar({ value = 0, label, indeterminate, tone }: Readonly<ProgressBarProps>) {
  const v = Math.max(0, Math.min(100, value));
  return (
    <div className={cx("gdg-progress", truthy(indeterminate) && "is-indeterminate", tone === "danger" && "is-error")}>
      {truthy(label) ? (
        <div className="gdg-progress__head">
          <span>{label}</span>
          {truthy(indeterminate) ? null : <span>{`${String(Math.round(v))}%`}</span>}
        </div>
      ) : null}
      <div
        className="gdg-progress__track"
        role="progressbar"
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={truthy(indeterminate) ? undefined : Math.round(v)}
        aria-label={nonEmpty(label) ?? "Progress"}
      >
        <div
          className="gdg-progress__fill"
          data-testid="progress-fill"
          style={truthy(indeterminate) ? undefined : { width: `${String(v)}%` }}
        />
      </div>
    </div>
  );
}

export function Spinner({ size = 18, label }: Readonly<SpinnerProps>) {
  return <span className="gdg-spinner" style={{ width: size, height: size }} role="status" aria-label={nonEmpty(label) ?? "Loading"} />;
}
