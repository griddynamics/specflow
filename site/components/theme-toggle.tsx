"use client";

import { useEffect, useState, type JSX } from "react";
import { currentTheme, storeTheme, storedTheme, type Theme } from "../lib/theme";

/** Light / dark switch. The page head sets `data-theme` before first paint; this keeps the button in step and remembers the choice. */
export function ThemeToggle(): JSX.Element {
  const [theme, setTheme] = useState<Theme>("light");

  useEffect(() => {
    const sync = () => setTheme(currentTheme());
    sync();
    const media = window.matchMedia("(prefers-color-scheme: dark)");
    const follow = (e: MediaQueryListEvent) => {
      if (storedTheme()) return;
      document.documentElement.dataset.theme = e.matches ? "dark" : "light";
      sync();
    };
    media.addEventListener("change", follow);
    return () => media.removeEventListener("change", follow);
  }, []);

  const toggle = () => {
    const next: Theme = currentTheme() === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    storeTheme(next);
    setTheme(next);
  };
  const dark = theme === "dark";

  return (
    <button
      className="theme-btn"
      type="button"
      onClick={toggle}
      aria-label={dark ? "Switch to light mode" : "Switch to dark mode"}
      title={dark ? "Light mode" : "Dark mode"}
    >
      <svg className="i-moon" viewBox="0 0 20 20" aria-hidden="true">
        <path d="M16 12.6A6.6 6.6 0 0 1 7.4 4a6.6 6.6 0 1 0 8.6 8.6z" />
      </svg>
      <svg className="i-sun" viewBox="0 0 20 20" aria-hidden="true">
        <circle cx="10" cy="10" r="3.4" />
        <path d="M10 1.8v2.2M10 16v2.2M1.8 10H4M16 10h2.2M4.2 4.2l1.6 1.6M14.2 14.2l1.6 1.6M4.2 15.8l1.6-1.6M14.2 5.8l1.6-1.6" />
      </svg>
    </button>
  );
}
