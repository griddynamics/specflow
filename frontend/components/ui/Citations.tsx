// FE-SPEC-03, FE-DS-02 (R5): `Citation` in bundle.js (a numbered source chip that opens the document) plus the row that holds
// them under an answer. `Citations` numbers its chips 1..n.
import { Icon } from "./Icon";

export interface CitationProps {
  label: string;
  index?: number;
  onClick?: () => void;
}

export function Citation({ label, index, onClick }: Readonly<CitationProps>) {
  return (
    <button type="button" className="gdg-cite" onClick={onClick} data-testid="citation">
      {index != null ? (
        <span className="gdg-cite__n" data-testid="citation-number">
          {index}
        </span>
      ) : null}
      <Icon name="file-md" size={14} />
      <span>{label}</span>
    </button>
  );
}

export function Citations({ citations }: Readonly<{ citations: { label: string; onClick?: () => void }[] }>) {
  if (!citations.length) return null;
  return (
    <div className="gdg-msg__cites">
      {citations.map((c, i) => (
        <Citation key={i} index={i + 1} {...c} />
      ))}
    </div>
  );
}
