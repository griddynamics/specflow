// FE-DS-02 (R5): port of `Breadcrumbs` in bundle.js. Earlier steps are buttons, the last one is the current page.
import "./ui-gaps.css";
import Link from "next/link";
import { Icon } from "./Icon";

export interface BreadcrumbsProps {
  /** An earlier step is a link when it has `href` (server pages), else a button with `onClick`. */
  items: { label: string; onClick?: () => void; href?: string }[];
  label?: string;
}

function Step({ item, last }: Readonly<{ item: BreadcrumbsProps["items"][number]; last: boolean }>) {
  if (last) return <span aria-current="page">{item.label}</span>;
  if (item.href !== undefined) return <Link href={item.href}>{item.label}</Link>;
  return (
    <button type="button" onClick={item.onClick}>
      {item.label}
    </button>
  );
}

export function Breadcrumbs({ items, label = "Breadcrumb" }: Readonly<BreadcrumbsProps>) {
  return (
    <nav className="gdg-crumbs" aria-label={label}>
      <ol>
        {items.map((it, i) => {
          const last = i === items.length - 1;
          return (
            <li key={i}>
              <Step item={it} last={last} />
              {last ? null : <Icon name="caret-right" size={12} />}
            </li>
          );
        })}
      </ol>
    </nav>
  );
}
