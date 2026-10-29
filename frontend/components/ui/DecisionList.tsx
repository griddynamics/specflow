// FE-DEC-01, FE-DS-02 (R5): port of `DecisionList` in bundle.js. A glass table: header row (decorative, aria-hidden), rows
// as <li> (DecisionRow), an empty message when there are none, and an optional footer ("4 of 28 decisions").
import { Children, type ReactNode } from "react";
import { GlassSurface } from "./GlassSurface";
import { nonEmpty, truthy } from "./utils";

export interface DecisionListProps {
  children?: ReactNode;
  columns?: string[];
  label?: string;
  empty?: ReactNode;
  footer?: ReactNode;
}

export function DecisionList({
  children,
  columns = ["Decision", "Domain", "Severity", "Assignee"],
  label,
  empty,
  footer,
}: Readonly<DecisionListProps>) {
  return (
    <GlassSurface variant="strong" className="gdg-dlist">
      <div className="gdg-dlist__head" aria-hidden>
        {columns.map((c, i) => (
          <span key={i}>{c}</span>
        ))}
        <span />
      </div>
      {Children.count(children) ? (
        <ul className="gdg-dlist__rows" aria-label={nonEmpty(label) ?? "Decisions"}>
          {children}
        </ul>
      ) : (
        <div className="gdg-dlist__empty">{truthy(empty) ? empty : "Nothing matches these filters."}</div>
      )}
      {truthy(footer) ? <div className="gdg-dlist__foot">{footer}</div> : null}
    </GlassSurface>
  );
}
