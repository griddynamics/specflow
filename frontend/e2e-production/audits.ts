// P18 T8: the page audits of P15 (fonts: p15-fonts, colour tokens: p15-tokens, keyboard ring walk: p15-keyboard) as plain functions that RETURN the
// violations, so the production-route audits (p18-audits.spec.ts) can reuse them. The P15 specs (p15-fonts, p15-tokens, p15-keyboard) use them too.
import type { Page } from "@playwright/test";

/** Every visible text run (and every form control) that is not set in Fira Sans. */
export function fontViolations(page: Page): Promise<string[]> {
  return page.evaluate(() => {
    const out = new Set<string>();
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    for (let n = walker.nextNode(); n; n = walker.nextNode()) {
      const el = n.parentElement;
      if (!el || !(n.textContent ?? "").trim() || ["SCRIPT", "STYLE"].includes(el.tagName) || el.closest("code, pre, kbd")) continue;
      const r = el.getBoundingClientRect();
      const cs = getComputedStyle(el);
      if (r.width === 0 || r.height === 0 || cs.visibility === "hidden") continue;
      const first = cs.fontFamily.split(",")[0].replace(/["']/g, "").trim();
      if (first !== "Fira Sans")
        out.add(
          `${el.tagName.toLowerCase()}.${(el.getAttribute("class") ?? "").split(" ")[0]} "${(n.textContent ?? "").trim().slice(0, 25)}" ${cs.fontFamily}`,
        );
    }
    for (const e of Array.from(document.querySelectorAll("input, textarea, select"))) {
      const r = e.getBoundingClientRect();
      if (r.width === 0 || (e as HTMLInputElement).type === "hidden" || (e as HTMLInputElement).type === "file") continue;
      const first = getComputedStyle(e).fontFamily.split(",")[0].replace(/["']/g, "").trim();
      if (first !== "Fira Sans") out.add(`${e.tagName.toLowerCase()} control font ${first}`);
    }
    return [...out];
  });
}

/** Text colours and backgrounds of visible elements that are not the resolved value of a colour token of the current theme. */
export function colourViolations(page: Page): Promise<{ tokens: number; looked: number; texts: number; bgs: number; bad: string[] }> {
  return page.evaluate(() => {
    const root = document.querySelector(".gdg-root") ?? document.body;
    // the custom property names declared by any readable style sheet
    const collectNames = (): Set<string> => {
      const names = new Set<string>();
      const walk = (list: CSSRuleList) => {
        for (const rule of Array.from(list)) {
          const st = (rule as CSSStyleRule).style as CSSStyleDeclaration | undefined;
          if (st !== undefined) for (const n of Array.from(st)) if (n.startsWith("--")) names.add(n);
          const nested = (rule as CSSGroupingRule).cssRules as CSSRuleList | undefined;
          if (nested !== undefined) walk(nested);
        }
      };
      for (const sheet of Array.from(document.styleSheets)) {
        try {
          walk(sheet.cssRules);
        } catch {
          continue;
        }
      }
      return names;
    };
    // resolved colour -> token name, for every colour token of the current theme
    const resolveTokens = (names: Set<string>): Map<string, string> => {
      const probe = document.createElement("i");
      root.appendChild(probe);
      const tokens = new Map<string, string>();
      for (const n of names) {
        probe.style.backgroundColor = "";
        probe.style.backgroundColor = `var(${n})`;
        const c = getComputedStyle(probe).backgroundColor;
        if (c && c !== "rgba(0, 0, 0, 0)") tokens.set(c, n);
      }
      probe.remove();
      return tokens;
    };
    const tokens = resolveTokens(collectNames());
    const isTransparent = (c: string) => c === "rgba(0, 0, 0, 0)" || /rgba\(.*,\s*0\)$/.test(c);
    const bad = new Set<string>();
    const seen = new Set<string>();
    const texts = new Set<string>();
    const bgs = new Set<string>();
    const hasOwnText = (el: HTMLElement) => Array.from(el.childNodes).some((n) => n.nodeType === 3 && (n.textContent ?? "").trim() !== "");
    const isSkipped = (el: HTMLElement, cs: CSSStyleDeclaration) => {
      const rect = el.getBoundingClientRect();
      return (
        rect.width === 0 ||
        rect.height === 0 ||
        cs.visibility === "hidden" ||
        el.closest(".gdg-conn__logo, svg, .react-flow__minimap") !== null
      );
    };
    for (const el of Array.from(document.querySelectorAll<HTMLElement>("body *"))) {
      const cs = getComputedStyle(el);
      if (isSkipped(el, cs)) continue;
      const label = `${el.tagName.toLowerCase()}.${(el.getAttribute("class") ?? "").split(" ")[0]}`;
      if (hasOwnText(el)) {
        seen.add(cs.color);
        texts.add(cs.color);
        if (!tokens.has(cs.color) && !isTransparent(cs.color)) bad.add(`text colour ${cs.color} on ${label}`);
      }
      if (!isTransparent(cs.backgroundColor)) {
        seen.add(cs.backgroundColor);
        bgs.add(cs.backgroundColor);
        if (!tokens.has(cs.backgroundColor)) bad.add(`background ${cs.backgroundColor} on ${label}`);
      }
    }
    return { tokens: tokens.size, looked: seen.size, texts: texts.size, bgs: bgs.size, bad: [...bad] };
  });
}

export interface Stop {
  id: string;
  label: string;
  ring: string;
  visible: boolean;
  inModal: boolean;
}

export interface TabWalkReport {
  stops: Stop[];
  lostMid: string[];
}

/**
 * Walks Tab from the top of the page until the focus wraps to the first stop, falls to <body>, or `max` stops; each stop reports its ring
 * (2px solid --focus-ring, on the control or on the wrapper that carries it through :focus-within) and visibility. `lostMid` lists the places where
 * the focus fell to <body> in the middle of the walk and the next Tab reached a control not seen before.
 */
export async function tabWalkReport(page: Page, max = 80): Promise<TabWalkReport> {
  await page.evaluate(() => {
    (document.activeElement as HTMLElement | null)?.blur();
    document.querySelectorAll("[data-kb]").forEach((e) => {
      e.removeAttribute("data-kb");
    });
    window.scrollTo(0, 0);
    // the sequential focus starting point goes to the top of the document, so Tab starts at the first control
    const start = document.createElement("div");
    start.tabIndex = -1;
    document.body.prepend(start);
    start.focus();
    start.remove();
  });
  const stops: Stop[] = [];
  const lostMid: string[] = [];
  for (let i = 0; i < max; i++) {
    await page.keyboard.press("Tab");
    const s = await page.evaluate(() => {
      const pick = (...parts: (string | null)[]) => parts.find((p) => p !== null && p !== "") ?? ""; // the first non-empty text, as an `||` chain would pick it
      const el = document.activeElement as HTMLElement | null;
      if (!el || el === document.body || el === document.documentElement) return { body: true } as const;
      if (el.hasAttribute("data-kb")) return { again: true } as const;
      el.setAttribute("data-kb", "1");
      const probe = document.createElement("i");
      probe.style.color = "var(--focus-ring)";
      (el.closest(".gdg-root") ?? el.parentElement ?? document.body).appendChild(probe); // inside the theme root: the token depends on it
      const token = getComputedStyle(probe).color;
      probe.remove();
      let ring = "none";
      for (let n: HTMLElement | null = el, d = 0; n && d < 5; n = n.parentElement, d++) {
        const cs = getComputedStyle(n);
        if (cs.outlineStyle === "solid" && cs.outlineWidth === "2px" && cs.outlineColor === token) {
          ring = `2px solid ${d === 0 ? "self" : "wrapper"} offset ${cs.outlineOffset}`;
          break;
        }
      }
      const r = el.getBoundingClientRect();
      const cs = getComputedStyle(el);
      const visible = r.width > 0 && r.height > 0 && cs.visibility !== "hidden" && cs.display !== "none" && Number(cs.opacity) > 0;
      const label = pick(el.getAttribute("aria-label"), el.getAttribute("title"), el.textContent, el.getAttribute("placeholder"))
        .trim()
        .replace(/\s+/g, " ")
        .slice(0, 40);
      const role = el.getAttribute("role");
      const roleSuffix = role === null || role === "" ? "" : "[" + role + "]";
      return {
        id: `${el.tagName.toLowerCase()}${roleSuffix}.${(el.getAttribute("class") ?? "").split(" ")[0]}`,
        label,
        ring,
        visible,
        inModal: !!el.closest("[role=dialog]"),
      };
    });
    if ("body" in s) {
      if (stops.length === 0) lostMid.push("first Tab went to <body>");
      else {
        // The end of the cycle goes through <body> and wraps to the first stop (seen). A NEW element after <body> means the focus was lost mid-walk.
        await page.keyboard.press("Tab");
        const fresh = await page.evaluate(() => {
          const el = document.activeElement as HTMLElement | null;
          return !!el && el !== document.body && !el.hasAttribute("data-kb");
        });
        if (fresh) lostMid.push(`focus fell to <body> after "${stops[stops.length - 1].label}" and the next Tab reached an unseen control`);
      }
      break;
    }
    if ("again" in s) break;
    stops.push(s);
  }
  return { stops, lostMid };
}

/** The stops of {@link tabWalkReport}. */
export async function tabWalk(page: Page, max = 80): Promise<Stop[]> {
  return (await tabWalkReport(page, max)).stops;
}
