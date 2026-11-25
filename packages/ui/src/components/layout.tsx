import type { JSX, ReactNode, SubmitEvent } from "react";
import { Tag } from "./tag";

/** A label and a count on one line, over a list. */
export function SectionHead({ title, note }: Readonly<{ title: string; note?: string }>): JSX.Element {
  return (
    <div className="gx-sectionhead">
      <b>{title}</b>
      {note === undefined ? null : <span>{note}</span>}
    </div>
  );
}

/** A field and its button on one row (the project name and Save, an email and Add). */
export function Split({ children }: Readonly<{ children: ReactNode }>): JSX.Element {
  return <div className="gx-split">{children}</div>;
}

/** A wrapping row of controls. */
export function Row({ children }: Readonly<{ children: ReactNode }>): JSX.Element {
  return <div className="gx-row">{children}</div>;
}

/** The bottom of a dialog: an action on the left, the others on the right. */
export function DialogFoot({ children }: Readonly<{ children: ReactNode }>): JSX.Element {
  return <div className="gx-foot">{children}</div>;
}

/** The page body under the header: a centred column. `busy` while it waits for its data. */
export function Page({ busy = false, children }: Readonly<{ busy?: boolean; children: ReactNode }>): JSX.Element {
  return (
    <main className="gx-page" aria-busy={busy ? true : undefined}>
      {children}
    </main>
  );
}

/** A short message in the page: `status` is read out politely, `alert` at once. */
export function Notice({ role = "status", children }: Readonly<{ role?: "status" | "alert"; children: ReactNode }>): JSX.Element {
  return (
    <p className="gx-notice" role={role}>
      {children}
    </p>
  );
}

/** A plain list of lines, each with a title and a detail (the open gaps). */
export function LineList({
  label,
  items,
}: Readonly<{ label: string; items: readonly { key: string; title: string; detail: string }[] }>): JSX.Element {
  return (
    <ul className="gx-lines" aria-label={label}>
      {items.map((item) => (
        <li key={item.key}>
          <b>{item.title}</b>
          <small>{item.detail}</small>
        </li>
      ))}
    </ul>
  );
}

/** The tools of an MCP server, one row each: the name in the mono face, then what asking for it does, in one sentence. */
export function ToolGrid({ label, items }: Readonly<{ label: string; items: readonly { name: string; does: string }[] }>): JSX.Element {
  return (
    <ul className="gx-tools" aria-label={label}>
      {items.map((item) => (
        <li key={item.name}>
          <code className="gx-tools__name">{item.name}</code>
          <span>{item.does}</span>
        </li>
      ))}
    </ul>
  );
}

export interface FileRowView {
  key: string;
  name: string;
  /** What sort of file it is ("PDF", "Markdown"). */
  format: string;
  /** A word, only for a file that is not done. */
  tag?: string;
  /** Why, under the row, for a file that has a note. */
  note?: string;
}

/** The files inside a source: name, kind, a word only when the file is not done. A long list scrolls with the dialog it is in. */
export function FileList({ label, rows }: Readonly<{ label: string; rows: readonly FileRowView[] }>): JSX.Element {
  return (
    <ul className="gx-files" aria-label={label}>
      {rows.map((row) => (
        <li key={row.key}>
          <span className="gx-files__name" title={row.name}>
            {row.name}
          </span>
          <span className="gx-files__meta">
            {row.format}
            {row.tag === undefined ? null : <Tag>{row.tag}</Tag>}
          </span>
          {row.note === undefined ? null : <small className="gx-files__why">{row.note}</small>}
        </li>
      ))}
    </ul>
  );
}

/** A native disclosure: a summary that is easy to press, and the content it folds. */
export function Disclosure({ summary, children }: Readonly<{ summary: string; children: ReactNode }>): JSX.Element {
  return (
    <details className="gx-disclosure">
      <summary className="gx-disclosure__summary">{summary}</summary>
      <div className="gx-disclosure__body">{children}</div>
    </details>
  );
}

/** A command or an id to copy, in the mono face. */
export function CodeLine({ children, inline = false }: Readonly<{ children: string; inline?: boolean }>): JSX.Element {
  return <code className={inline ? "gx-code gx-code--inline" : "gx-code"}>{children}</code>;
}

/** Children one under the other with the dialog's gap. With `onSubmit` it is a form (Enter submits). */
export function Stack({ onSubmit, children }: Readonly<{ onSubmit?: (event: SubmitEvent) => void; children: ReactNode }>): JSX.Element {
  return onSubmit === undefined ? (
    <div className="gx-stack">{children}</div>
  ) : (
    <form className="gx-stack" onSubmit={onSubmit} noValidate>
      {children}
    </form>
  );
}

/** The frame of a signed-in screen: at least the height of the window, the header on top. */
export function AppFrame({ children }: Readonly<{ children: ReactNode }>): JSX.Element {
  return <div className="gx-frame">{children}</div>;
}
