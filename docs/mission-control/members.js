/* The header's project parts (DECISIONS.md D11, D12).
   Members dialog: the project's people and name (docs/projects/ui.md), in one dialog opened from the header.
   The owner renames the project, adds people by email (with suggestions), removes members and cancels invites. A collaborator sees the
   list read-only and can leave the project. Every removal asks first.
   Project menu: the project name opens a list of projects with "Create new project" under it; a person with no project gets
   "Create your first project", which cannot be closed. Demo data only. */
(() => {
  "use strict";
  const LIMIT = 50;
  const $ = (id) => document.getElementById(id);
  const mdl = $("mdl"), mdlIn = $("mdl-in"), opener = $("members-open");
  const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
  const initials = (s) => s.split(/[\s.@]+/).filter(Boolean).slice(0, 2).map((w) => w[0].toUpperCase()).join("");
  /* People Graphico already knows: an email of one of them adds a member; any other email saves an invite */
  const PEOPLE = [
    { name: "Jordan Lee", email: "jordan.lee@griddynamics.com" },
    { name: "Priya Shah", email: "priya.shah@griddynamics.com" },
    { name: "Marta Nowak", email: "marta.nowak@griddynamics.com" },
    { name: "Daniel Okafor", email: "daniel.okafor@griddynamics.com" },
    { name: "Sofia Rossi", email: "sofia.rossi@griddynamics.com" },
  ];
  const M = {
    role: "owner",
    name: "Northwind ordering",
    members: [
      { id: "u1", name: "Alex Kim", email: "alex.kim@griddynamics.com", owner: true },
      { id: "u2", name: "Jordan Lee", email: "jordan.lee@griddynamics.com" },
      { id: "u3", name: "Priya Shah", email: "priya.shah@griddynamics.com" },
    ],
    invites: [{ id: "i1", email: "sam.ortiz@griddynamics.com" }],
    draft: null, text: "", error: "", confirm: null, say: "",
  };
  let uid = 10;

  const suggestions = () => {
    const q = M.text.trim().toLowerCase();
    if (q.length < 3) return [];
    const taken = new Set([...M.members.map((m) => m.email), ...M.invites.map((i) => i.email)]);
    return PEOPLE.filter((p) => !taken.has(p.email) && (p.name.toLowerCase().includes(q) || p.email.includes(q))).slice(0, 8);
  };
  /* The prototype's "view as" switch: the owner is Alex, the collaborator is Jordan */
  const youId = () => (M.role === "owner" ? "u1" : "u2");
  const row = (r, owner) => {
    const invite = !r.name;
    const action = owner && !r.owner ? `<button class="m-x" type="button" data-m-x="${r.id}" aria-label="${invite ? "Cancel invite" : "Remove"} ${esc(r.name || r.email)}" title="${invite ? "Cancel invite" : "Remove"}">×</button>` : "";
    return `<li class="m-row"><span class="m-av${invite ? " inv" : ""}" aria-hidden="true">${invite ? "@" : esc(initials(r.name))}</span>
      <span class="m-who"><b>${esc(r.name || r.email)}${r.id === youId() ? " (you)" : ""}</b><small>${invite ? "Not signed in yet" : esc(r.email)}</small></span>
      ${r.owner ? '<span class="tag">Owner</span>' : invite ? '<span class="tag m-inv">Invited</span>' : "<span></span>"}${action}</li>`;
  };
  const confirmBlock = () => {
    const c = M.confirm; if (!c) return "";
    const who = c.kind === "leave" ? "" : esc(c.label);
    const [title, body, verb] = c.kind === "leave" ? ["Leave this project?", "You lose access to it. Your story claims are freed.", "Leave project"]
      : c.kind === "invite" ? ["Cancel this invite?", `${who} will not get access when they sign in.`, "Cancel invite"]
      : ["Remove this collaborator?", `${who} loses access to this project. Their story claims are freed.`, "Remove"];
    return `<div class="m-confirm" role="alertdialog" aria-labelledby="m-c-t"><b id="m-c-t">${title}</b><p class="small">${body}</p>
      <div class="row"><button class="btn sm ghost" type="button" data-m-keep autofocus>Keep</button><button class="btn sm" type="button" data-m-confirm>${verb}</button></div></div>`;
  };

  function render(focusSel) {
    const owner = M.role === "owner";
    const draft = M.draft ?? M.name, trimmed = draft.trim();
    const canSave = owner && trimmed.length > 0 && trimmed !== M.name;
    const list = [...M.members, ...M.invites];
    mdlIn.innerHTML = `<div class="mdl-top"><div><p class="eyebrow">Project</p><h2 id="mdl-title">Members</h2></div><button class="mdl-x" type="button" data-close aria-label="Close">×</button></div>
      ${owner ? `<div class="m-name"><div class="field"><label for="m-name">Project name</label><input id="m-name" value="${esc(draft)}" maxlength="80" autocomplete="off"></div><button class="btn ghost" type="button" data-m-save ${canSave ? "" : "disabled"}>Save</button></div>` : ""}
      <div class="m-head"><b>Collaborators</b><span class="small muted">${list.length} of ${LIMIT}</span></div>
      ${owner ? `<div class="m-add"><div class="field"><label class="sr-only" for="m-email">Collaborator email</label>
          <input id="m-email" type="email" placeholder="Add by email" value="${esc(M.text)}" autocomplete="off" role="combobox" aria-expanded="false" aria-controls="m-sugg"${M.error ? ' aria-invalid="true" aria-describedby="m-err"' : ""}></div>
          <button class="btn" type="button" data-m-add ${M.text.trim() ? "" : "disabled"}>Add</button>
          <ul class="m-sugg" id="m-sugg" role="listbox" aria-label="Suggested people" hidden></ul>
        </div>${M.error ? `<p class="small m-err" id="m-err" role="alert">${esc(M.error)}</p>` : ""}` : ""}
      <ul class="m-list" aria-label="Collaborators">${list.map((r) => row(r, owner)).join("")}</ul>
      ${confirmBlock()}
      ${owner ? "" : M.confirm ? "" : '<div><button class="btn sm ghost" type="button" data-m-leave>Leave project</button></div>'}
      <p class="small" aria-live="polite">${esc(M.say)}</p>
      <div class="mdl-foot"><span class="proto-ctl"><span>Prototype · view as</span><span class="seg" role="group" aria-label="View as"><button type="button" data-m-role="owner" aria-pressed="${owner}">Owner</button><button type="button" data-m-role="member" aria-pressed="${!owner}">Collaborator</button></span></span><button class="btn ghost" type="button" data-close>Done</button></div>`;
    typed();
    const f = focusSel && mdlIn.querySelector(focusSel);
    if (f) { f.focus(); if (f.setSelectionRange && f.value) f.setSelectionRange(f.value.length, f.value.length); }
  }

  /* Typing updates only the suggestions and the Add button, so the field keeps its cursor */
  function typed() {
    const ul = $("m-sugg"), input = $("m-email"); if (!ul || !input) return;
    const sugg = suggestions();
    ul.innerHTML = sugg.map((p) => `<li role="option"><button type="button" data-m-pick="${esc(p.email)}"><span class="m-av" aria-hidden="true">${esc(initials(p.name))}</span><span class="m-who"><b>${esc(p.name)}</b><small>${esc(p.email)}</small></span></button></li>`).join("");
    ul.hidden = sugg.length === 0; input.setAttribute("aria-expanded", String(sugg.length > 0));
    mdlIn.querySelector("[data-m-add]").disabled = !M.text.trim();
  }
  function add() {
    const email = M.text.trim().toLowerCase();
    if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email)) { M.error = "Enter an email address."; return render("#m-email"); }
    if ([...M.members, ...M.invites].some((r) => r.email === email)) { M.error = "This person is already on the project."; return render("#m-email"); }
    if (M.members.length + M.invites.length >= LIMIT) { M.error = `A project has at most ${LIMIT} collaborators.`; return render("#m-email"); }
    const known = PEOPLE.find((p) => p.email === email);
    if (known) { M.members.push({ id: "u" + ++uid, ...known }); M.say = "Collaborator added"; }
    else { M.invites.push({ id: "i" + ++uid, email }); M.say = "Invite saved. They get access when they first sign in."; }
    M.text = ""; M.error = "";
    render("#m-email");
  }

  opener.addEventListener("click", () => {
    Object.assign(M, { draft: null, text: "", error: "", confirm: null, say: "" });
    render();
    if (!mdl.open) mdl.showModal();
    (mdlIn.querySelector("#m-email") || mdlIn.querySelector(".mdl-x")).focus();
  });

  mdl.addEventListener("click", (e) => {
    const t = e.target.closest("button"); if (!t || !mdlIn.querySelector("#mdl-title")?.textContent.startsWith("Members")) return;
    const d = t.dataset;
    if ("mSave" in d) {
      const v = (M.draft ?? M.name).trim(); if (!v || v === M.name) return;
      PROJECTS[PROJECTS.indexOf(M.name)] = v; M.name = v; M.draft = null; M.say = "Project renamed";
      showName(v);
      render("#m-name");
    } else if ("mAdd" in d) add();
    else if (d.mPick) { M.text = d.mPick; M.error = ""; render("#m-email"); }
    else if (d.mX) {
      const r = [...M.members, ...M.invites].find((x) => x.id === d.mX);
      M.confirm = { kind: r.name ? "remove" : "invite", id: r.id, label: r.name || r.email }; M.say = ""; render("[data-m-keep]");
    } else if ("mLeave" in d) { M.confirm = { kind: "leave" }; render("[data-m-keep]"); }
    else if ("mKeep" in d) { M.confirm = null; render(); }
    else if ("mConfirm" in d) {
      const c = M.confirm; M.confirm = null;
      if (c.kind === "leave") { M.say = "You left the project. In the app this opens your next project."; M.members = M.members.filter((m) => m.id !== youId()); }
      else if (c.kind === "invite") { M.invites = M.invites.filter((i) => i.id !== c.id); M.say = "Invite cancelled"; }
      else { M.members = M.members.filter((m) => m.id !== c.id); M.say = "Collaborator removed"; }
      render();
    } else if (d.mRole) { M.role = d.mRole; M.confirm = null; M.say = ""; render(); }
  });
  mdl.addEventListener("input", (e) => {
    if (e.target.id === "m-name") { M.draft = e.target.value; const b = mdlIn.querySelector("[data-m-save]"); const v = M.draft.trim(); if (b) b.disabled = !v || v === M.name; }
    if (e.target.id === "m-email") {
      M.text = e.target.value;
      if (M.error) { M.error = ""; mdlIn.querySelector("#m-err")?.remove(); e.target.removeAttribute("aria-invalid"); }
      typed();
    }
  });
  mdl.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && e.target.id === "m-email" && !$("m-sugg").hidden) { e.preventDefault(); $("m-sugg").hidden = true; return; } // Esc closes only the list
    if (e.key !== "Enter") return;
    if (e.target.id === "m-email") { e.preventDefault(); add(); }
    if (e.target.id === "m-name") { e.preventDefault(); mdlIn.querySelector("[data-m-save]")?.click(); }
  });

  /* ---------- Project menu and new projects ---------- */
  const PROJECTS = ["Northwind ordering", "Payments revamp", "Fleet tracking"];
  const projBtn = document.querySelector(".proj"), menu = document.createElement("ul");
  const showName = (v) => document.querySelectorAll(".proj").forEach((p) => { p.firstChild.textContent = v; });
  let firstRun = false;
  menu.className = "proj-menu"; menu.id = "proj-menu"; menu.hidden = true; menu.setAttribute("aria-label", "Projects");
  projBtn.after(menu);
  projBtn.setAttribute("aria-haspopup", "true"); projBtn.setAttribute("aria-expanded", "false"); projBtn.setAttribute("aria-controls", "proj-menu");
  function openMenu(on) {
    menu.hidden = !on; projBtn.setAttribute("aria-expanded", String(on));
    if (!on) return;
    menu.innerHTML = PROJECTS.map((p) => `<li><button type="button" data-pj="${esc(p)}" aria-current="${p === M.name}">${esc(p)}</button></li>`).join("")
      + '<li class="sep" role="separator"></li><li><button type="button" data-pj-new>+ Create new project</button></li>';
    menu.querySelector("button").focus();
  }
  function switchTo(name) {
    Object.assign(M, { name, role: "owner", draft: null });
    showName(name);
  }
  function openCreate(first) {
    firstRun = first;
    mdlIn.innerHTML = `<div class="mdl-top"><div><p class="eyebrow">${first ? "Welcome to Graphico" : "New project"}</p><h2 id="mdl-title">${first ? "Create your first project" : "Create a project"}</h2></div>${first ? "" : '<button class="mdl-x" type="button" data-close aria-label="Close">×</button>'}</div>
      <p class="intro">A project holds the sources, the graph, the gaps and the plan of one piece of work. Invite people from Members once it exists.</p>
      <div class="field"><label for="pj-name">Project name</label><input id="pj-name" maxlength="80" autocomplete="off" placeholder="For example: Northwind ordering"></div>
      <div class="mdl-foot">${first ? "<span></span>" : '<button class="linkbtn" type="button" data-close>Cancel</button>'}<button class="btn" type="button" data-pj-create disabled>Create project</button></div>`;
    if (!mdl.open) mdl.showModal();
    $("pj-name").focus();
  }
  function create() {
    const v = $("pj-name").value.trim(); if (!v || PROJECTS.includes(v)) return;
    PROJECTS.push(v);
    Object.assign(M, { members: [{ id: "u1", name: "Alex Kim", email: "alex.kim@griddynamics.com", owner: true }], invites: [] });
    switchTo(v); firstRun = false; mdl.close();
    window.dispatchEvent(new CustomEvent("gr:new-project"));
  }
  projBtn.addEventListener("click", (e) => { e.stopPropagation(); openMenu(menu.hidden); });
  document.addEventListener("click", (e) => { if (!menu.hidden && !menu.contains(e.target)) openMenu(false); });
  menu.addEventListener("keydown", (e) => {
    const items = [...menu.querySelectorAll("button")], i = items.indexOf(document.activeElement);
    if (e.key === "Escape") { openMenu(false); projBtn.focus(); }
    else if (e.key === "ArrowDown" || e.key === "ArrowUp") { e.preventDefault(); items[(i + (e.key === "ArrowDown" ? 1 : items.length - 1)) % items.length].focus(); }
  });
  menu.addEventListener("click", (e) => {
    const t = e.target.closest("button"); if (!t) return;
    openMenu(false);
    if ("pjNew" in t.dataset) openCreate(false); else { switchTo(t.dataset.pj); projBtn.focus(); }
  });
  /* The first-run window cannot be closed: no ×, no Esc, no click outside */
  mdl.addEventListener("click", (e) => { if (firstRun && e.target === mdl) e.stopImmediatePropagation(); }, true);
  mdl.addEventListener("cancel", (e) => { if (firstRun) e.preventDefault(); });
  mdl.addEventListener("close", () => { firstRun = false; });
  mdl.addEventListener("click", (e) => { if (e.target.closest("[data-pj-create]")) create(); });
  mdl.addEventListener("input", (e) => { if (e.target.id === "pj-name") { const v = e.target.value.trim(); mdlIn.querySelector("[data-pj-create]").disabled = !v || PROJECTS.includes(v); } });
  mdl.addEventListener("keydown", (e) => { if (e.key === "Enter" && e.target.id === "pj-name") { e.preventDefault(); create(); } });
  window.addEventListener("gr:first-run", () => openCreate(true));
})();
