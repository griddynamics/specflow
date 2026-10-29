// FE-DEC-03, FE-DS-02 (R5): port of `DocLinks` in bundle.js. One button per source document: file icon, title, "path · section",
// external-link icon. A link's own `onClick` wins over the list-wide `onOpen`.
import { Icon } from "./Icon";
import "./ui-gaps-app.css";
import { truthy } from "./utils";

export interface DocLink {
  title: string;
  path: string;
  section?: string;
  onClick?: () => void;
}

export interface DocLinksProps {
  links: DocLink[];
  onOpen?: (link: { title: string; path: string; section?: string }) => void;
}

export function DocLinks({ links, onOpen }: Readonly<DocLinksProps>) {
  return (
    <ul className="gdg-doclinks" data-testid="doc-links">
      {links.map((l, i) => (
        <li key={i}>
          <button type="button" onClick={l.onClick ?? (() => onOpen?.(l))}>
            <Icon name="file-md" size={18} />
            <span>
              <b>{l.title}</b>
              <small>{l.path + (truthy(l.section) ? ` · ${l.section}` : "")}</small>
            </span>
            <Icon name="arrow-square-out" size={14} />
          </button>
        </li>
      ))}
    </ul>
  );
}
