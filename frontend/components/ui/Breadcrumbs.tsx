// FE-DS-02 (R5): port of `Breadcrumbs` in bundle.js. Earlier steps are buttons, the last one is the current page.
import "./ui-gaps.css";
import { Icon } from "./Icon";

export interface BreadcrumbsProps {
  items: { label: string; onClick?: () => void }[];
  label?: string;
}

export function Breadcrumbs({ items, label = "Breadcrumb" }: Readonly<BreadcrumbsProps>) {
  return (
    <nav className="gdg-crumbs" aria-label={label}>
      <ol>
        {items.map((it, i) => {
          const last = i === items.length - 1;
          return (
            <li key={i}>
              {last ? (
                <span aria-current="page">{it.label}</span>
              ) : (
                <button type="button" onClick={it.onClick}>
                  {it.label}
                </button>
              )}
              {last ? null : <Icon name="caret-right" size={12} />}
            </li>
          );
        })}
      </ol>
    </nav>
  );
}
