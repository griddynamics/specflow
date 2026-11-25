"use client";
// The theme switch of the header. The theme lives on `<html data-theme>` (root layout, from the `specflow-theme` cookie); switching sets
// both, so the next page load starts in the same theme. Light unless the cookie says dark (lib/theme.ts): the OS preference is ignored.
import { useSyncExternalStore } from "react";
import { writeCookie } from "@/lib/storage";
import { THEME_COOKIE, themeFromCookie, type Theme } from "@/lib/theme";

const YEAR_SECONDS = 31_536_000;

function subscribe(onChange: () => void): () => void {
  const observer = new MutationObserver(onChange);
  observer.observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
  return () => {
    observer.disconnect();
  };
}

const current = (): Theme => themeFromCookie(document.documentElement.dataset.theme);
const serverTheme = (): Theme => "light";

export function useTheme(): { theme: Theme; toggle: () => void } {
  const theme = useSyncExternalStore(subscribe, current, serverTheme);
  const toggle = () => {
    const next: Theme = theme === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    writeCookie(THEME_COOKIE, next, YEAR_SECONDS);
  };
  return { theme, toggle };
}
