"use client";

import { useEffect, useId, useRef, useState } from "react";
import { focusLeftMenu } from "./menu-focus";
import type { FocusEvent, JSX, KeyboardEvent, ReactNode } from "react";

export interface MenuItem {
  key: string;
  label: string;
  /** The item stands for what is chosen now (a project list): it is announced as checked. */
  current?: boolean;
  /** A line above this item. */
  separatorBefore?: boolean;
  onSelect: () => void;
}

export interface MenuProps {
  /** What the trigger shows. */
  trigger: ReactNode;
  /** Accessible name of the trigger and the list. */
  label: string;
  /** `project`: the name with a chevron; `avatar`: a round button. */
  look?: "project" | "avatar";
  items: readonly MenuItem[];
}

const TRIGGER_CLASS = { project: "gx-menu__trigger gx-menu__trigger--project", avatar: "gx-menu__trigger gx-menu__trigger--avatar" };

/** A dropdown of actions. Escape and a click outside close it; arrows move; the focus goes back to the trigger. */
export function Menu({ trigger, label, look = "project", items }: Readonly<MenuProps>): JSX.Element {
  const [open, setOpen] = useState(false);
  const root = useRef<HTMLSpanElement>(null);
  const list = useRef<HTMLUListElement>(null);
  // A press inside the list: browsers that do not focus a button on click (Safari, Firefox on macOS) blur the list with no `relatedTarget`
  // before the click lands, so a blur during a press is not "the focus left".
  const pressing = useRef(false);
  const id = useId();

  useEffect(() => {
    if (!open) return;
    list.current?.querySelector<HTMLElement>("button")?.focus();
    const away = (e: PointerEvent) => {
      if (root.current !== null && e.target instanceof Node && !root.current.contains(e.target)) setOpen(false);
    };
    const released = () => {
      requestAnimationFrame(() => {
        pressing.current = false; // after the click that follows the release
      });
    };
    document.addEventListener("pointerdown", away);
    document.addEventListener("pointerup", released);
    return () => {
      document.removeEventListener("pointerdown", away);
      document.removeEventListener("pointerup", released);
    };
  }, [open]);

  const close = () => {
    setOpen(false);
    root.current?.querySelector<HTMLElement>(".gx-menu__trigger")?.focus();
  };
  // The focus left the list for something outside the menu (Tab, a click elsewhere): close it, and leave the focus where it went.
  const onBlur = (e: FocusEvent) => {
    const inside = e.relatedTarget instanceof Node && root.current?.contains(e.relatedTarget) === true;
    if (focusLeftMenu(pressing.current, inside)) setOpen(false);
  };
  const onKey = (e: KeyboardEvent) => {
    if (e.key === "Escape") {
      e.preventDefault();
      close();
      return;
    }
    if (!["ArrowDown", "ArrowUp", "Home", "End"].includes(e.key)) return;
    e.preventDefault();
    const buttons = [...(list.current?.querySelectorAll<HTMLElement>("button") ?? [])];
    const at = buttons.indexOf(document.activeElement as HTMLElement);
    const step = e.key === "ArrowDown" ? 1 : buttons.length - 1;
    const next = { Home: 0, End: buttons.length - 1 }[e.key] ?? (at + step) % buttons.length;
    buttons[next]?.focus();
  };

  return (
    <span className="gx-menu" ref={root}>
      <button
        type="button"
        className={TRIGGER_CLASS[look]}
        aria-label={look === "project" && typeof trigger === "string" && trigger !== label ? `${label}: ${trigger}` : label}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-controls={id}
        onClick={() => {
          setOpen((o) => !o);
        }}
      >
        {trigger}
        {look === "project" ? (
          <svg viewBox="0 0 12 12" aria-hidden="true" focusable="false">
            <path d="M2.5 4.5 6 8l3.5-3.5" />
          </svg>
        ) : null}
      </button>
      {open ? (
        <ul
          className="gx-menu__list"
          id={id}
          role="menu"
          aria-label={label}
          ref={list}
          onKeyDown={onKey}
          onBlur={onBlur}
          onPointerDown={() => {
            pressing.current = true;
          }}
        >
          {items.map((item) => (
            <li key={item.key} role="none" className={item.separatorBefore === true ? "gx-menu__sep" : undefined}>
              <button
                type="button"
                role={item.current === undefined ? "menuitem" : "menuitemradio"}
                aria-checked={item.current}
                className="gx-menu__item"
                onClick={() => {
                  close();
                  item.onSelect();
                }}
              >
                {item.label}
                {item.current === true ? (
                  <svg className="gx-menu__check" viewBox="0 0 12 12" aria-hidden="true" focusable="false">
                    <path d="M2.5 6.5 5 9l4.5-5.5" />
                  </svg>
                ) : null}
              </button>
            </li>
          ))}
        </ul>
      ) : null}
    </span>
  );
}
