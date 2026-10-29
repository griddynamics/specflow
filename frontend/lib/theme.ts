// FE-DS-07 (TECH-SPECS §7): theme cookie. Light unless the cookie says dark; the OS preference is ignored.
export const THEME_COOKIE = "specflow-theme";
export type Theme = "light" | "dark";
export const themeFromCookie = (value: string | undefined): Theme => (value === "dark" ? "dark" : "light");
