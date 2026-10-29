"use client";
// FE-DS-02, FE-DOD-05.AC5 (R5): port of `Menu` in bundle.js. Opens below the trigger; closes on choice, outside click or
// Esc; arrow keys move between items. Addition (GAPS P04): Esc returns the focus to the trigger.
import { useEffect, useId, useRef, useState, type KeyboardEvent, type ReactNode } from "react";
import "./ui-gaps.css";
import { Avatar, type AvatarProps } from "./Avatar";
import { GlassSurface } from "./GlassSurface";
import { Icon, type IconName } from "./Icon";
import { focusedElement, useOutside } from "./use-outside";
import { cx, truthy } from "./utils";

export interface MenuItem {
  label?: string;
  icon?: IconName;
  avatar?: AvatarProps;
  description?: string;
  checked?: boolean;
  tone?: "danger";
  shortcut?: string;
  disabled?: boolean;
  onSelect?: (item: MenuItem) => void;
  section?: string;
  divider?: boolean;
}

export interface MenuProps {
  /** Content of the default pill trigger, or a render function for a custom trigger. */
  trigger: ReactNode | ((t: { open: boolean; toggle: () => void; id: string }) => ReactNode);
  items: MenuItem[];
  header?: ReactNode;
  align?: "start" | "end";
  label?: string;
  onSelect?: (item: MenuItem) => void;
  defaultOpen?: boolean;
  triggerClassName?: string;
  className?: string;
}

const ITEMS = "[role=menuitem],[role=menuitemradio]";
const FIRST = "[role=menuitem]:not(:disabled),[role=menuitemradio]:not(:disabled)";

export function Menu({
  trigger,
  items,
  header,
  align = "start",
  label,
  onSelect,
  defaultOpen,
  triggerClassName,
  className,
}: Readonly<MenuProps>) {
  const [open, setOpen] = useState(defaultOpen === true);
  const ref = useRef<HTMLDivElement>(null);
  const listRef = useRef<HTMLDivElement>(null);
  const id = useId();
  const [autoFocus, setAutoFocus] = useState(false); // true once the user opens it; a menu that starts open keeps the focus where it is
  const focusTrigger = () => ref.current?.querySelector<HTMLElement>("[aria-haspopup]")?.focus();
  const closeMenu = () => {
    setOpen(false);
    focusTrigger();
  };

  // Outside mousedown and Esc close the menu (bundle.js `useOutside`). Esc also puts the focus back on the trigger.
  useOutside(ref, open, (reason) => {
    setOpen(false);
    if (reason === "escape") focusTrigger();
  });

  // Opening by the user moves the focus to the first item (not when the menu starts open, as in the previews).
  useEffect(() => {
    if (open && autoFocus) listRef.current?.querySelector<HTMLElement>(FIRST)?.focus();
  }, [open, autoFocus]);

  function onKey(e: KeyboardEvent<HTMLElement>) {
    if (e.key !== "ArrowDown" && e.key !== "ArrowUp") return;
    e.preventDefault();
    const els = Array.from(listRef.current?.querySelectorAll<HTMLElement>(ITEMS) ?? []);
    if (!els.length) return;
    const i = els.findIndex((el) => el === focusedElement());
    const n = e.key === "ArrowDown" ? (i + 1) % els.length : (i - 1 + els.length) % els.length;
    els[n].focus();
  }

  const toggle = () => {
    if (!open) setAutoFocus(true);
    setOpen(!open);
  };
  // Like a native select: ArrowDown on a closed trigger opens the list.
  const onTriggerKey = (e: KeyboardEvent<HTMLElement>) => {
    if (e.key !== "ArrowDown" || open) return;
    e.preventDefault();
    setAutoFocus(true);
    setOpen(true);
  };
  const triggerNode =
    typeof trigger === "function" ? (
      trigger({ open, toggle, id })
    ) : (
      <button
        type="button"
        className={cx("gdg-menu__trigger", triggerClassName)}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-controls={id}
        onClick={toggle}
        onKeyDown={onTriggerKey}
      >
        {trigger}
        <Icon name="caret-down" size={14} className="gdg-menu__caret" />
      </button>
    );

  return (
    <div className={cx("gdg-menu", `gdg-menu--${align}`, className)} ref={ref}>
      {triggerNode}
      {open ? (
        <GlassSurface variant="strong" floating className="gdg-menu__list" role="menu" id={id} aria-label={label} onKeyDown={onKey}>
          <div ref={listRef} style={{ display: "contents" }}>
            {header ?? null}
            {items.map((it, i) => {
              if (truthy(it.divider)) return <div key={i} className="gdg-menu__divider" role="separator" />;
              if (truthy(it.section))
                return (
                  <div key={i} className="gdg-menu__section">
                    {it.section}
                  </div>
                );
              const radio = it.checked !== undefined;
              return (
                <button
                  key={i}
                  type="button"
                  role={radio ? "menuitemradio" : "menuitem"}
                  aria-checked={radio ? it.checked === true : undefined}
                  tabIndex={-1}
                  className={cx("gdg-menu__item", it.tone === "danger" && "is-danger")}
                  disabled={it.disabled}
                  onClick={() => {
                    it.onSelect?.(it);
                    onSelect?.(it);
                    closeMenu(); // choosing returns the focus to the trigger, like Esc
                  }}
                >
                  {it.icon ? <Icon name={it.icon} size={18} /> : null}
                  {!it.icon && it.avatar ? <Avatar size="sm" {...it.avatar} /> : null}
                  <span className="gdg-menu__text">
                    <span>{it.label}</span>
                    {truthy(it.description) ? <small>{it.description}</small> : null}
                  </span>
                  {radio && truthy(it.checked) ? <Icon name="check" size={16} className="gdg-menu__check" /> : null}
                  {truthy(it.shortcut) ? <kbd>{it.shortcut}</kbd> : null}
                </button>
              );
            })}
          </div>
        </GlassSurface>
      ) : null}
    </div>
  );
}
