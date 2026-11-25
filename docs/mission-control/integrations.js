/* Integrations page: one connection per app for the project (DECISIONS.md D1-D4). It works with the account of the member
   who set it up; when that account loses access, what uses it stops. What each app is used for (a repository, a channel,
   a Jira epic) is added and edited on the canvas, so the page only connects. A card is grey until set up, then ink. Demo data. */
(() => {
  "use strict";
  const { icon } = window.SFIcons;
  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
  const mdl = $("mdl"), mdlIn = $("mdl-in"), grid = $("ig-grid");
  const YOU = "Alex Kim";

  /* The GitHub organization's repositories the app can read; the project picks some of them */
  const ORG_REPOS = ["northwind/ordering", "northwind/ordering-web", "northwind/deploy", "northwind/infra", "northwind/design-docs"];
  const APPS = [
    { id: "atlassian", name: "Atlassian", sub: "Jira and Confluence", marks: ["jira", "confluence"], button: "Connect with Atlassian", site: "northwind.atlassian.net" },
    { id: "github", name: "GitHub", sub: "Repositories", marks: ["github"], button: "Connect GitHub", site: "northwind", repos: true },
    { id: "slack", name: "Slack", sub: "Channels", marks: ["slack"], button: "Add to Slack", site: "Northwind workspace" },
    { id: "notion", name: "Notion", sub: "Pages and databases", marks: ["notion"], button: "Connect Notion", site: "Northwind workspace" },
    { id: "linear", name: "Linear", sub: "Projects and issues", marks: ["linear"], button: "Connect Linear", site: "Northwind team" },
    { id: "telegram", name: "Telegram", sub: "Groups", marks: ["telegram"], button: "Add the Graphico bot", site: "@GraphicoBot" },
  ];

  /* Which apps are connected for this project, by whom, and (GitHub) which repositories. Kept in this browser (localStorage)
     so Mission Control can tell; a fresh browser starts with GitHub connected. */
  const KEY = "gr-integrations";
  const load = () => { try { const v = JSON.parse(localStorage.getItem(KEY)); if (v && typeof v === "object") return v; } catch (e) { /* storage blocked or empty */ } return null; };
  const S = load() || { github: { by: YOU, repos: ORG_REPOS.slice(0, 3) } };
  const save = () => { try { localStorage.setItem(KEY, JSON.stringify(S)); } catch (e) { /* the choice lasts for this page only */ } };
  let edit = null;

  /* ---------- Cards ---------- */
  function card(a) {
    const on = S[a.id];
    const foot = a.soon ? '<span class="small muted">Next step</span>'
      : `<button class="btn sm ${on ? "ghost" : ""}" type="button" data-setup="${a.id}">${on ? "Manage" : "Set up"}</button>${on ? `<span class="small muted">by ${esc(on.by)}</span>` : ""}`;
    return `<li class="ig-card" data-id="${a.id}" data-on="${!!on}" data-soon="${!!a.soon}">
      <div class="ig-top"><span class="ig-logos">${a.marks.map((m) => `<span class="ig-logo">${icon(m)}</span>`).join("")}</span>${on ? '<span class="tag ig-status">Active</span>' : ""}</div>
      <div><h2 class="ig-name">${esc(a.name)}</h2><p class="ig-sub">${esc(a.sub)}${on && a.repos ? ` · ${on.repos.length}` : ""}</p></div>
      <div class="ig-foot">${foot}</div>
    </li>`;
  }
  const render = () => { grid.innerHTML = APPS.map(card).join(""); };

  /* ---------- Set-up dialog: connect, and for GitHub pick the project's repositories ---------- */
  function openSetup(id) {
    const cur = S[id];
    edit = { id, connected: !!cur, by: cur?.by || YOU, repos: [...(cur?.repos || [])], confirm: false };
    renderSetup(".mdl-x");
    if (!mdl.open) mdl.showModal();
  }
  function renderSetup(focusSel) {
    const a = APPS.find((x) => x.id === edit.id), cur = S[a.id];
    const ok = edit.connected && (!a.repos || edit.repos.length > 0);
    const conn = edit.connected
      ? `<p class="ig-ok"><span class="ig-dot" aria-hidden="true"></span>${esc(a.site)} · by ${esc(edit.by)}</p>`
      : `<div><button class="btn" type="button" data-ig-connect>${esc(a.button)}</button></div>`;
    const repos = a.repos && edit.connected ? `<fieldset class="pick"><legend class="eyebrow">Repositories</legend>${ORG_REPOS.map((r) => `<label class="pick-row"><input type="checkbox" data-ig-repo="${esc(r)}" ${edit.repos.includes(r) ? "checked" : ""}><span>${esc(r)}</span></label>`).join("")}</fieldset>` : "";
    const confirm = edit.confirm ? `<div class="m-confirm" role="alertdialog" aria-labelledby="ig-c-t"><b id="ig-c-t">Disconnect ${esc(a.name)}?</b>
      <p class="small">What uses it on the canvas stops.</p>
      <div class="row"><button class="btn sm ghost" type="button" data-ig-keep autofocus>Keep</button><button class="btn sm" type="button" data-ig-off>Disconnect</button></div></div>` : "";
    mdlIn.innerHTML = `<div class="mdl-top"><div><p class="eyebrow">Integration${back ? " · then back to Mission Control" : ""}</p><h2 id="mdl-title">${esc(a.name)}</h2></div><button class="mdl-x" type="button" data-close aria-label="Close">×</button></div>
      ${conn}${repos}
      <p class="small muted">Uses ${edit.by === YOU ? "your" : esc(edit.by) + "'s"} account for this project. If its access ends, what uses it stops.</p>
      ${confirm}
      <div class="mdl-foot">${cur && !edit.confirm ? '<button class="linkbtn" type="button" data-ig-disconnect>Disconnect</button>' : "<span></span>"}
        <span class="row"><button class="linkbtn" type="button" data-close>Cancel</button><button class="btn" type="button" data-ig-save ${ok ? "" : "disabled"}>${cur ? "Save" : "Turn on"}</button></span></div>`;
    (mdlIn.querySelector(focusSel || "") || mdlIn.querySelector(".mdl-x")).focus();
  }

  /* ---------- Events ---------- */
  grid.addEventListener("click", (e) => {
    const c = e.target.closest(".ig-card"); if (!c || c.dataset.soon === "true") return;
    openSetup(c.dataset.id);
  });
  mdl.addEventListener("click", (e) => {
    if (e.target === mdl) { mdl.close(); return; }
    const t = e.target.closest("button"); if (!t) return;
    if (t.matches("[data-close]")) { mdl.close(); return; }
    if (!edit || !mdlIn.querySelector("[data-ig-save]")) return; // the Members and project dialogs handle their own buttons
    const d = t.dataset;
    if ("igConnect" in d) {
      t.disabled = true; t.textContent = "Connecting…";
      setTimeout(() => { edit.connected = true; edit.by = YOU; if (APPS.find((x) => x.id === edit.id).repos && !edit.repos.length) edit.repos = [ORG_REPOS[0]]; renderSetup("[data-ig-save]"); }, 700);
    }
    else if ("igSave" in d) {
      S[edit.id] = { by: edit.by, ...(edit.repos.length ? { repos: [...edit.repos] } : {}) }; save(); edit = null; mdl.close(); render();
      /* Sent here from a plus on the canvas: go back and finish adding the connection there */
      if (back) location.href = `index.html#add=${encodeURIComponent(back)}`;
    }
    else if ("igDisconnect" in d) { edit.confirm = true; renderSetup("[data-ig-keep]"); }
    else if ("igKeep" in d) { edit.confirm = false; renderSetup(); }
    else if ("igOff" in d) { delete S[edit.id]; save(); edit = null; mdl.close(); render(); }
  });
  mdl.addEventListener("change", (e) => {
    const r = e.target.dataset.igRepo; if (!edit || !r) return;
    edit.repos = e.target.checked ? [...edit.repos, r] : edit.repos.filter((x) => x !== r);
    renderSetup(`[data-ig-repo="${r}"]`);
  });
  mdl.addEventListener("close", () => { if (!mdl.open) edit = null; }); // the close event is queued: ignore it if the dialog was opened again

  $("ig-mcp-ic").innerHTML = icon("mcp");
  render();
  /* integrations.html#setup=slack&back=slack: Mission Control sent someone here to set an app up first */
  const h = new URLSearchParams(location.hash.slice(1)), want = h.get("setup"), back = h.get("back");
  if (want && APPS.some((a) => a.id === want && !a.soon)) {
    openSetup(want);
  }
})();
