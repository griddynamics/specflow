import type { JSX, MouseEvent, ReactNode } from "react";

export interface AppHeaderProps {
  /** The brand (the mark and the word). */
  brand: ReactNode;
  /** Beside the brand: the project menu. */
  project?: ReactNode;
  /** Accessible name of the navigation. */
  navLabel: string;
  /** The navigation: links, buttons, the theme switch and the account menu. */
  children?: ReactNode;
}

/** The app header: brand and project on the left, the navigation on the right. Sticky, one row. */
export function AppHeader({ brand, project, navLabel, children }: Readonly<AppHeaderProps>): JSX.Element {
  return (
    <header className="gx-header">
      <div className="gx-header__in">
        {brand}
        {project === undefined ? null : <span className="gx-header__project">{project}</span>}
        <nav className="gx-nav" aria-label={navLabel}>
          {children}
        </nav>
      </div>
    </header>
  );
}

export interface NavLinkProps {
  href: string;
  /** The page the person is on. */
  current?: boolean;
  /** Called on a plain click; call `preventDefault` to navigate in the app instead of loading the page. */
  onNavigate?: (event: MouseEvent<HTMLAnchorElement>) => void;
  children: ReactNode;
}

const isPlainClick = (e: MouseEvent): boolean => e.button === 0 && !e.metaKey && !e.ctrlKey && !e.shiftKey && !e.altKey;

/** A link of the header navigation. */
export function NavLink({ href, current = false, onNavigate, children }: Readonly<NavLinkProps>): JSX.Element {
  return (
    <a
      className="gx-nav__item"
      href={href}
      aria-current={current ? "page" : undefined}
      onClick={(e) => {
        if (onNavigate !== undefined && isPlainClick(e)) onNavigate(e);
      }}
    >
      {children}
    </a>
  );
}

export interface NavButtonProps {
  onClick: () => void;
  /** The button opens a dialog. */
  opensDialog?: boolean;
  children: ReactNode;
}

/** A button of the header navigation (it looks like a link). */
export function NavButton({ onClick, opensDialog = false, children }: Readonly<NavButtonProps>): JSX.Element {
  return (
    <button type="button" className="gx-nav__item" aria-haspopup={opensDialog ? "dialog" : undefined} onClick={onClick}>
      {children}
    </button>
  );
}

export interface ThemeToggleProps {
  /** The theme in force now. */
  theme: "light" | "dark";
  onToggle: () => void;
  /** Accessible names: what pressing it does. */
  toDarkLabel: string;
  toLightLabel: string;
}

/** The light and dark switch: a bare icon after a separator. */
export function ThemeToggle({ theme, onToggle, toDarkLabel, toLightLabel }: Readonly<ThemeToggleProps>): JSX.Element {
  return (
    <button type="button" className="gx-theme" aria-label={theme === "dark" ? toLightLabel : toDarkLabel} onClick={onToggle}>
      <svg viewBox="0 0 20 20" aria-hidden="true" focusable="false">
        {theme === "dark" ? (
          <>
            <circle cx="10" cy="10" r="3.4" />
            <path d="M10 1.8v2.2M10 16v2.2M1.8 10H4M16 10h2.2M4.2 4.2l1.6 1.6M14.2 14.2l1.6 1.6M4.2 15.8l1.6-1.6M14.2 5.8l1.6-1.6" />
          </>
        ) : (
          <path d="M16 12.6A6.6 6.6 0 0 1 7.4 4a6.6 6.6 0 1 0 8.6 8.6z" />
        )}
      </svg>
    </button>
  );
}
