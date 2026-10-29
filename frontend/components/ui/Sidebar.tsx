"use client";
// FE-SHELL-01, FE-DS-08, FE-DS-02 (R5): port of `Sidebar` in bundle.js. Floating glass nav: full (264px) or icon rail (76px).
// The logo paths come from props (default: the copies in public/ds/Logos), never from window.GDGlassAssets. Collapsed items
// carry `aria-label` and `data-tip`; the tip shows on hover and keyboard focus (bundle.css). Toggle returns through `onToggle`.
import type { CSSProperties } from "react";
import "./ui-gaps-app.css"; // also carries the Prose link ring: the app shell always renders a Sidebar (Prose itself stays CSS-free for node tests)
import { GlassSurface } from "./GlassSurface";
import { Icon, type IconName } from "./Icon";
import { ThemeLogo } from "./ThemeLogo";
import { sidebarInitials } from "./app-logic";
import { useControlled } from "./use-controlled";
import { cx, nonEmpty } from "./utils";

export type SidebarEntry = { value: string; label: string; icon: IconName; badge?: number | string } | { section: string };

export interface SidebarProps {
  items: SidebarEntry[];
  value?: string;
  defaultValue?: string;
  onChange?: (value: string) => void;
  /** Icon rail (76px) instead of icons + text (264px). */
  collapsed?: boolean;
  defaultCollapsed?: boolean;
  onToggle?: (collapsed: boolean) => void;
  user?: { name: string; role?: string };
  /** Expanded logo for the light theme. Defaults to the copy in public/ds/Logos. */
  logoSrc?: string;
  /** Expanded logo for the dark theme. Defaults to the copy in public/ds/Logos. */
  logoDarkSrc?: string;
  /** Mark shown when collapsed, both themes. Defaults to the copy in public/ds/Logos. */
  markSrc?: string;
  label?: string;
  className?: string;
  style?: CSSProperties;
}

export const SIDEBAR_WORDMARK = "/ds/Logos/full-color-black-horizontal.svg";
export const SIDEBAR_WORDMARK_DARK = "/ds/Logos/full-color-white-horizontal.svg";
export const SIDEBAR_MARK = "/ds/Logos/full-color-black-mark.svg";

export function Sidebar({
  items,
  value,
  defaultValue,
  onChange,
  collapsed: collapsedProp,
  defaultCollapsed,
  onToggle,
  user,
  logoSrc,
  logoDarkSrc,
  markSrc,
  label,
  className,
  style,
}: Readonly<SidebarProps>) {
  const [collapsed, setCollapsed] = useControlled<boolean>(collapsedProp, defaultCollapsed === true, onToggle);
  const [cur, setCur] = useControlled<string | undefined>(value, defaultValue, (v) => v !== undefined && onChange?.(v));
  return (
    <GlassSurface
      as="aside"
      className={cx("gdg-side", collapsed && "gdg-side--collapsed", className)}
      floating
      aria-label={nonEmpty(label) ?? "Primary"}
      style={style}
    >
      <div className="gdg-side__head">
        {collapsed ? (
          <img className="gdg-side__mark" src={nonEmpty(markSrc) ?? SIDEBAR_MARK} alt="Grid Dynamics" width={32} height={32} />
        ) : (
          <ThemeLogo
            className="gdg-side__logo"
            light={nonEmpty(logoSrc) ?? SIDEBAR_WORDMARK}
            dark={nonEmpty(logoDarkSrc) ?? SIDEBAR_WORDMARK_DARK}
            alt="Grid Dynamics"
          />
        )}
      </div>
      <nav className="gdg-side__nav">
        {items.map((it, i) => {
          if ("section" in it) {
            return collapsed ? (
              <div key={`s${String(i)}`} className="gdg-side__rule" role="separator" />
            ) : (
              <div key={`s${String(i)}`} className="gdg-side__section">
                {it.section}
              </div>
            );
          }
          const on = cur === it.value;
          return (
            <button
              key={it.value}
              type="button"
              className="gdg-side__item"
              aria-current={on ? "page" : undefined}
              aria-label={collapsed ? it.label : undefined}
              data-tip={collapsed ? it.label : undefined}
              onClick={(e) => {
                e.currentTarget.setAttribute("data-quiet", "1"); // P15 (QA m10): the tip of a clicked item stays hidden until the pointer leaves it
                setCur(it.value);
              }}
              onMouseLeave={(e) => {
                e.currentTarget.removeAttribute("data-quiet");
              }}
              onBlur={(e) => {
                e.currentTarget.removeAttribute("data-quiet");
              }}
            >
              <Icon name={it.icon} size={20} />
              {collapsed ? null : <span className="gdg-side__label">{it.label}</span>}
              {it.badge != null ? <span className="gdg-side__badge">{it.badge}</span> : null}
            </button>
          );
        })}
      </nav>
      <div className="gdg-side__foot">
        {user ? (
          <div className="gdg-side__user" data-tip={collapsed ? user.name : undefined}>
            <span className="gdg-side__avatar" aria-hidden>
              {sidebarInitials(user.name)}
            </span>
            {collapsed ? null : (
              <span className="gdg-side__who">
                <b>{user.name}</b>
                <small>{user.role}</small>
              </span>
            )}
          </div>
        ) : null}
        <button
          type="button"
          className="gdg-side__item gdg-side__toggle"
          onClick={() => {
            setCollapsed(!collapsed);
          }}
          aria-label={collapsed ? "Expand sidebar" : "Collapse sidebar"}
          aria-expanded={!collapsed}
          data-tip={collapsed ? "Expand" : undefined}
        >
          <Icon name={collapsed ? "caret-double-right" : "caret-double-left"} size={18} />
          {collapsed ? null : <span className="gdg-side__label">Collapse</span>}
        </button>
      </div>
    </GlassSurface>
  );
}
