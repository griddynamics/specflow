/* Graphico landing drafts: shared behaviour (menu, tabs, show/hide toggles, demo forms, copy buttons). */
(() => {
  "use strict";

  /* Light / dark switch. The page head sets data-theme before first paint; this keeps it and the button in step. */
  const root = document.documentElement;
  const store = (t) => { try { localStorage.setItem("sf-theme", t); } catch (e) { /* storage blocked: the choice lasts for this page only */ } };
  const stored = () => { try { return localStorage.getItem("sf-theme"); } catch (e) { return null; } };
  const sysDark = window.matchMedia("(prefers-color-scheme: dark)");
  const themeBtns = [...document.querySelectorAll("[data-theme-toggle]")];
  const syncTheme = () => themeBtns.forEach((b) => { const dark = root.dataset.theme === "dark"; b.setAttribute("aria-label", dark ? "Switch to light mode" : "Switch to dark mode"); b.title = dark ? "Light mode" : "Dark mode"; });
  if (!root.dataset.theme) root.dataset.theme = sysDark.matches ? "dark" : "light";
  themeBtns.forEach((b) => b.addEventListener("click", () => { const t = root.dataset.theme === "dark" ? "light" : "dark"; root.dataset.theme = t; store(t); syncTheme(); }));
  sysDark.addEventListener("change", (e) => { if (!stored()) { root.dataset.theme = e.matches ? "dark" : "light"; syncTheme(); } });
  syncTheme();

  /* Mobile menu */
  document.querySelectorAll("[data-menu-btn]").forEach((btn) => {
    const nav = document.getElementById(btn.getAttribute("aria-controls"));
    if (!nav) return;
    const set = (open) => { nav.classList.toggle("is-open", open); btn.setAttribute("aria-expanded", String(open)); btn.textContent = open ? "Close" : "Menu"; };
    btn.addEventListener("click", () => set(!nav.classList.contains("is-open")));
    nav.addEventListener("click", (e) => { if (e.target.closest("a")) set(false); });
    document.addEventListener("keydown", (e) => { if (e.key === "Escape" && nav.classList.contains("is-open")) { set(false); btn.focus(); } });
  });

  /* Tabs: [data-tabs] holds [role=tab] buttons with aria-controls; panels are [role=tabpanel] */
  function selectTab(tab, focus) {
    const list = tab.closest("[role=tablist]");
    list.querySelectorAll("[role=tab]").forEach((t) => {
      const on = t === tab;
      t.setAttribute("aria-selected", String(on));
      t.tabIndex = on ? 0 : -1;
      const panel = document.getElementById(t.getAttribute("aria-controls"));
      if (panel) panel.hidden = !on;
    });
    if (focus) tab.focus();
    list.dispatchEvent(new CustomEvent("tabchange", { detail: { id: tab.id, value: tab.dataset.value }, bubbles: true }));
  }
  document.querySelectorAll("[role=tablist]").forEach((list) => {
    const tabs = [...list.querySelectorAll("[role=tab]")];
    tabs.forEach((t) => {
      t.addEventListener("click", () => selectTab(t, false));
      t.addEventListener("keydown", (e) => {
        const i = tabs.indexOf(t);
        let j = null;
        if (e.key === "ArrowRight" || e.key === "ArrowDown") j = (i + 1) % tabs.length;
        if (e.key === "ArrowLeft" || e.key === "ArrowUp") j = (i - 1 + tabs.length) % tabs.length;
        if (e.key === "Home") j = 0;
        if (e.key === "End") j = tabs.length - 1;
        if (j !== null) { e.preventDefault(); selectTab(tabs[j], true); }
      });
    });
    const start = tabs.find((t) => t.getAttribute("aria-selected") === "true") || tabs[0];
    if (start) selectTab(start, false);
  });
  window.GraphicoTabs = { select: selectTab };

  /* Show/hide toggles: [data-toggle] with aria-controls; optional data-open-label / data-closed-label */
  document.querySelectorAll("[data-toggle]").forEach((btn) => {
    const ids = btn.getAttribute("aria-controls").split(/\s+/);
    const sync = () => {
      const open = !document.getElementById(ids[0]).hidden;
      btn.setAttribute("aria-expanded", String(open));
      if (btn.dataset.openLabel) btn.textContent = open ? btn.dataset.openLabel : btn.dataset.closedLabel;
    };
    btn.addEventListener("click", () => {
      ids.forEach((id) => { const el = document.getElementById(id); if (el) el.hidden = !el.hidden; });
      sync();
    });
    sync();
  });

  /* Demo forms: nothing is sent from a draft */
  document.querySelectorAll("form[data-demo-form]").forEach((form) => {
    const status = form.querySelector("[data-form-status]");
    form.addEventListener("submit", (e) => {
      e.preventDefault();
      if (!form.checkValidity()) { form.reportValidity(); if (status) status.textContent = "Fill in the fields marked as required."; return; }
      const name = (form.querySelector("[name=name]") || {}).value || "";
      if (status) status.textContent = `Thanks${name ? ", " + name.split(" ")[0] : ""}. This is a draft page, so nothing was sent.`;
      form.querySelectorAll("input, select, textarea, button[type=submit]").forEach((el) => { el.disabled = true; });
    });
  });

  /* Copy buttons */
  document.querySelectorAll("[data-copy]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const src = document.getElementById(btn.dataset.copy);
      const text = src ? src.textContent.trim() : "";
      const done = (msg) => { const old = btn.textContent; btn.textContent = msg; setTimeout(() => { btn.textContent = old; }, 1600); };
      const fallback = () => {
        const range = document.createRange(); range.selectNodeContents(src);
        const sel = window.getSelection(); sel.removeAllRanges(); sel.addRange(range);
        done("Selected");
      };
      try {
        if (navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(text).then(() => done("Copied"), fallback);
        else fallback();
      } catch (err) { fallback(); }
    });
  });

  document.querySelectorAll("[data-year]").forEach((el) => { el.textContent = String(new Date().getFullYear()); });
})();
