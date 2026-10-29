// FE-MC-08.AC5, FE-REV-01.AC3: scroll helpers shared by the Files view and the README links. DOM only; call from effects and
// event handlers (never during render).

/** The nearest ancestor that scrolls vertically, or null when the page itself scrolls. */
export function scrollParent(el: HTMLElement): HTMLElement | null {
  for (let p = el.parentElement; p; p = p.parentElement) {
    const o = getComputedStyle(p).overflowY;
    if ((o === "auto" || o === "scroll") && p.scrollHeight > p.clientHeight) return p;
  }
  return null;
}

/** Scrolls the element with `id` to the top of its scroll container (or the page) and moves focus to it (`tabIndex=-1` headings). */
export function scrollToSection(id: string, root: ParentNode = document, behavior: ScrollBehavior = "auto"): boolean {
  const el = root instanceof Document ? root.getElementById(id) : root.querySelector<HTMLElement>(`[id="${CSS.escape(id)}"]`);
  if (!el) return false;
  const box = scrollParent(el);
  if (box) {
    const pad = Number.parseFloat(getComputedStyle(box).paddingTop) || 0;
    box.scrollTo({ top: box.scrollTop + el.getBoundingClientRect().top - box.getBoundingClientRect().top - pad, behavior });
  } else {
    el.scrollIntoView({ block: "start", behavior });
  }
  if (el.tabIndex >= -1) el.focus({ preventScroll: true });
  return true;
}
