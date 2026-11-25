import type { JSX, ReactNode } from "react";

/** A circle with a question mark: the same mark on both modes. */
function HelpGlyph(): JSX.Element {
  return (
    <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.6" aria-hidden>
      <circle cx="8" cy="8" r="6.5" />
      <path d="M6.1 6.2A2 2 0 0 1 8 4.8c1.1 0 2 .8 2 1.8 0 1.4-2 1.5-2 3" strokeLinecap="round" />
      <circle cx="8" cy="11.6" r="0.5" fill="currentColor" />
    </svg>
  );
}

export interface ShowMeHowProps {
  /** The words on the control ("Show me how"). */
  label: string;
  /** With `href` it is a link to a page; without it, a disclosure that opens `children` inline (closed at first). */
  href?: string;
  children?: ReactNode;
}

/** Help for a step the person does outside Graphico: one look, two modes. */
export function ShowMeHow({ label, href, children }: Readonly<ShowMeHowProps>): JSX.Element {
  if (href !== undefined) {
    return (
      <div className="gx-how">
        <a className="gx-how__face" href={href}>
          <HelpGlyph />
          {label}
        </a>
      </div>
    );
  }
  return (
    <details className="gx-how">
      <summary className="gx-how__face">
        <HelpGlyph />
        {label}
      </summary>
      <div className="gx-how__body">{children}</div>
    </details>
  );
}
