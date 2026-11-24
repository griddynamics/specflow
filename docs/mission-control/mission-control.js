/* Mission Control prototype: one canvas, three zones (Graph, Gap, Plan). Decisions behind it: DECISIONS.md.
   The state below drives every node, wire and text; a 200 ms tick moves imports, builds and syncs.
   Vendor marks are the simple-icons paths the app already uses (one colour, here drawn in ink). Slack has no mark there,
   so it shows a channel sign. Everything is invented demo data. */
(() => {
  "use strict";

  /* ---------- Icons ---------- */
  /* Vendor marks and app glyphs are shared with the Integrations page (icons.js) */
  const { MARK, GLYPH } = window.SFIcons;
  /* Hub glyphs, 52 x 52 */
  const HUB = {
    graph: '<circle class="f" cx="26" cy="26" r="5"/><circle cx="12" cy="14" r="3.6"/><circle cx="40" cy="12" r="3.6"/><circle cx="42" cy="36" r="3.6"/><circle cx="14" cy="40" r="3.6"/><circle cx="26" cy="6.5" r="2.6"/><path d="M15 16l7 6.5M37.3 14.3 30 22.4M39 34.3l-8.6-5.6M17 37.8l5.3-7.4M26 9v12M15.6 14.2 23.4 7.8M14 36.4l-1.6-18.6"/>',
    brain: '<path d="M26 9.5v33M26 12.5c-2-4.5-9.5-5-11.5 0-4.8.4-7.2 5.3-5.2 9-3.6 3-3 9 1.2 10.8-.2 5.2 5.6 8.6 10 6.2 1.8 2.4 4.2 3 5.5 2.5M26 12.5c2-4.5 9.5-5 11.5 0 4.8.4 7.2 5.3 5.2 9 3.6 3 3 9-1.2 10.8.2 5.2-5.6 8.6-10 6.2-1.8 2.4-4.2 3-5.5 2.5M14.5 12.5c.5 3 2.6 4.6 5.5 4.5M9.3 21.5c2.3 1.6 5.3 1.4 7.2-.4M10.5 32.3c2.6-.4 4.7-2.3 5-5M37.5 12.5c-.5 3-2.6 4.6-5.5 4.5M42.7 21.5c-2.3 1.6-5.3 1.4-7.2-.4M41.5 32.3c-2.6-.4-4.7-2.3-5-5M20.5 38.5c.3-2.8 2.4-4.4 5.5-4.5M31.5 38.5c-.3-2.8-2.4-4.4-5.5-4.5"/>',
    gap: '<circle cx="22" cy="22" r="13"/><path d="m32 32 10.5 10.5"/><path d="M18 18.5a4.2 4.2 0 1 1 5.6 4c-1.1.4-1.6 1.2-1.6 2.3v.7"/><circle class="f" cx="22" cy="29.6" r="1.4"/>',
    plan: '<rect x="8" y="9" width="36" height="34" rx="5"/><path d="M8 18h36M17 9v34"/><path d="m22 25.5 2.2 2.2 4.3-4.3M32 25.5h7M22 34.5l2.2 2.2 4.3-4.3M32 34.5h7"/>',
  };
  const icon = (kind, cls = "") =>
    MARK[kind] ? `<svg class="logo ${cls}" viewBox="0 0 24 24" aria-hidden="true"><path d="${MARK[kind]}"/></svg>`
      : `<svg class="glyph ${cls}" viewBox="0 0 24 24" aria-hidden="true">${GLYPH[kind] || ""}</svg>`;
  const iconKind = (k) => ({ jirasrc: "jira", jiragap: "jira", ghplan: "github" })[k] || k;
  const CHECK = '<svg viewBox="0 0 12 12"><path d="M2.5 6.2 5 8.6 9.5 3.6"/></svg>';
  const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);

  /* ---------- What can be connected where. now = works today, progress = being built, next = the next step ---------- */
  /* Every object is its own node: two repositories are two nodes, two Confluence links are two nodes (DECISIONS.md D5) */
  const KINDS = {
    upload: { name: "Upload", zone: "knowledge", avail: "now", line: "PDF, Word, PowerPoint, Excel, Markdown, CSV" },
    gdrive: { name: "Google Drive", zone: "knowledge", avail: "now", line: "A shared folder, read when it is added" },
    github: { name: "GitHub repository", zone: "knowledge", avail: "now", line: "Code, docs and READMEs of one repository" },
    confluence: { name: "Confluence link", zone: "knowledge", avail: "now", line: "A page or a space, following its links" },
    jirasrc: { name: "Jira epic", zone: "knowledge", avail: "now", line: "An epic and every issue under it" },
    notion: { name: "Notion page", zone: "knowledge", avail: "now", line: "A page or a database, with its sub-pages" },
    obsidian: { name: "Obsidian vault", zone: "knowledge", avail: "now", line: "A vault of Markdown notes, following their links" },
    slack: { name: "Slack channel", zone: "gap", avail: "now", line: "Gaps go to a channel; answers come back in the thread" },
    jiragap: { name: "Jira", zone: "gap", avail: "now", line: "Gaps as a story; each sub-task is a problem to solve" },
    telegram: { name: "Telegram group", zone: "gap", avail: "now", line: "Gaps go to a group; answers come back in the replies" },
    jira: { name: "Jira", zone: "plan", avail: "now", line: "Stories and sub-tasks under an epic, kept up to date" },
    ghplan: { name: "GitHub Projects", zone: "plan", avail: "now", line: "The plan as issues of a repository's project" },
    linear: { name: "Linear", zone: "plan", avail: "now", line: "The plan as issues of a Linear project" },
  };
  const ZONE = {
    knowledge: { n: "01", title: "Add a source", noun: "source", intro: "Graphico imports what you add, once, and builds one graph from it. A changed document is added again." },
    gap: { n: "02", title: "Add a gap channel", noun: "channel", intro: "Graphico asks its questions where the people who can answer already talk, and reads the answers back." },
    plan: { n: "03", title: "Add a plan destination", noun: "destination", intro: "The plan goes where the team tracks work, and is kept up to date there." },
  };
  const AVAIL = { now: "Available", progress: "In progress", next: "Next step" };
  const ZONE_NAME = { knowledge: "Graph", gap: "Gap", plan: "Plan" };
  /* MCP joins the canvas by itself when its stage has something for coding agents: the gaps, then the plan (docs/mcp-server/tools.md) */
  const MCP = {
    gap: { meta: "Agents answer gaps", tools: ["list_gaps", "browse_gaps", "open_gap", "answer_gap", "add_finding"], prompt: "/mcp__graphico__answer_gaps", say: "Coding agents see the open gaps of their work, answer the ones they can, and report new findings." },
    plan: { meta: "Agents claim stories", tools: ["list_areas", "list_stories", "claim_story", "get_story_brief", "report_task", "complete_story"], prompt: "/mcp__graphico__start", say: "Coding agents claim a whole story, read its brief and report every task as they build it." },
  };
  const BUILD_STEPS = ["Convert the files", "Take a snapshot", "Split and compare", "Find the things, then the statements", "Merge duplicates", "Find the links",
    "Check and enforce the rules", "Compare with the stored graph", "Write", "Add inferred links", "Verify and repair"];
  const LEVELS = ["Low", "Medium", "High", "Extra high", "Max"];
  const FACTOR = [0.6, 1.0, 1.4, 2.0, 2.8];
  const estimate = (docs, lvl) => { const b = (2 + 0.4 * docs) * FACTOR[lvl - 1]; const lo = Math.max(1, Math.round(b * 0.7)); const hi = Math.max(lo + 1, Math.round(b * 1.5)); return hi > 60 ? "more than an hour" : `about ${lo}–${hi} minutes`; };

  /* ---------- State ---------- */
  let S, uid = 0, level = 2;
  const fresh = () => ({
    sources: [], channels: [], dests: [],
    k: { building: false, p: 0, built: false, stale: false, items: 0 },
    gaps: { on: false, total: 0, critical: 0, asked: 0, answered: 0, target: 0, flowT: 0 },
    plan: { building: false, p: 0, on: false, stale: false, stories: 0, tasks: 0, syncT: 0, syncIn: 0 },
  });
  const src = (kind, name, files, status = "importing", done = 0) => ({ id: "n" + ++uid, kind, name, files, status, done, listT: 0 });
  const filesOf = (list) => list.reduce((n, s) => n + (s.status === "ok" ? s.files : 0), 0);
  const importing = () => S.sources.filter((s) => s.status === "listing" || s.status === "importing");
  const stopped = () => S.sources.find((s) => s.status === "stopped");
  const kReady = () => S.sources.length > 0 && importing().length === 0 && !S.k.building && (!S.k.built || S.k.stale);
  /* The plan waits for a person too: once no critical gap is open, someone decides to build it */
  const gapsDone = () => S.gaps.on && S.gaps.asked >= S.gaps.target;
  /* The plan is the second decision: it is ready as soon as the gaps are found, answered or not.
     After a rebuild or new answers it is ready again as a replan: never rebuilt, only refined (DECISIONS.md D9). */
  const pReady = () => S.k.built && !S.k.building && gapsDone() && !S.plan.building && (!S.plan.on || S.plan.stale);

  /* Eight scenes; a scene is a preset of the state. The tick then runs it on. */
  const SCENES = [
    { name: "Nothing connected", set() { S = fresh(); } },
    { name: "Importing sources", set() { S = fresh(); S.sources = [src("gdrive", "Specs folder", 24, "importing", 6), src("upload", "Kick-off pack", 6, "importing", 4), src("github", "ordering", 18, "listing"), src("github", "deploy", 7, "listing")]; addOut(); } },
    { name: "Ready to build", set() { S = fresh(); S.sources = [src("gdrive", "Specs folder", 24, "ok"), src("upload", "Kick-off pack", 6, "ok"), src("github", "ordering", 18, "ok"), src("github", "deploy", 7, "ok")]; addOut(); } },
    { name: "Building the graph", set() { SCENES[2].set(); S.k.building = true; S.k.p = 0.05; } },
    { name: "Gaps found · ready to plan", set() { SCENES[2].set(); Object.assign(S.k, { built: true, items: 1412 }); Object.assign(S.gaps, { on: true, total: 14, critical: 3, asked: 9, answered: 2, target: 9 }); } },
    { name: "Plan in Jira", set() { SCENES[2].set(); Object.assign(S.k, { built: true, items: 1418 }); Object.assign(S.gaps, { on: true, total: 14, critical: 0, asked: 14, answered: 9, target: 14 }); Object.assign(S.plan, { on: true, stories: 6, tasks: 31, syncT: 0, syncIn: 40 }); } },
    { name: "New information · replan", set() { SCENES[5].set(); Object.assign(S.gaps, { total: 16, answered: 10, asked: 16, target: 16 }); S.plan.stale = true; } },
    /* The account behind an integration lost access: what uses it stops until someone reconnects (DECISIONS.md D2) */
    { name: "A connection stopped", set() { SCENES[5].set(); S.sources.filter((s) => s.kind === "github").forEach((s) => { s.status = "stopped"; }); } },
  ];
  function addOut() { S.channels = [src("slack", "#northwind-specs", 0, "ok")]; S.dests = [src("jira", "Jira · NWO-1", 0, "ok")]; }
  function sceneIndex() {
    if (!S.sources.length) return 0;
    if (stopped()) return 7;
    if (importing().length) return 1;
    if (S.k.building) return 3;
    if (!S.k.built || S.k.stale) return 2;
    if (S.plan.on && S.plan.stale) return 6;
    return S.plan.on || S.plan.building ? 5 : 4;
  }

  /* ---------- DOM ---------- */
  const $ = (id) => document.getElementById(id);
  const canvas = $("canvas"), wires = $("wires"), bg = $("bg"), mdl = $("mdl"), mdlIn = $("mdl-in");
  const cols = { sources: $("col-sources"), khub: $("col-khub"), ghub: $("col-ghub"), channels: $("col-channels"), phub: $("col-phub"), dests: $("col-dests") };
  const els = new Map();

  function nodeEl(id, kind) {
    let el = els.get(id);
    if (el) return el;
    el = document.createElement("div");
    el.className = "node"; el.dataset.id = id;
    if (kind === "mcp") el.classList.add("auto");
    el.innerHTML = `<button class="tile" type="button">${icon(iconKind(kind))}<span class="badge"></span></button><span class="nm"></span><span class="mt"></span><span class="bar"><i></i></span>${kind === "mcp" ? '<span class="tag">Automatic</span>' : ""}`;
    els.set(id, el);
    return el;
  }
  function hubEl(id) {
    let el = els.get(id);
    if (el) return el;
    el = document.createElement("div");
    el.className = "hub"; el.dataset.id = id;
    el.innerHTML = `<button class="hub-tile" type="button"><svg class="hub-ring" viewBox="0 0 146 146"><circle class="t" cx="73" cy="73" r="70"/><circle class="p" cx="73" cy="73" r="70" stroke-dasharray="439.8" stroke-dashoffset="439.8"/></svg><svg class="glyph" viewBox="0 0 52 52" aria-hidden="true"></svg></button><span class="pill"></span><span class="hub-text"><span class="cta"></span></span>`;
    els.set(id, el);
    return el;
  }
  function addEl(zone, big) {
    const id = "add-" + zone;
    let el = els.get(id);
    if (!el) {
      el = document.createElement("button");
      el.type = "button"; el.className = "add"; el.dataset.add = zone; el.dataset.id = id;
      el.innerHTML = `<span class="plus" aria-hidden="true">+</span><span>Add ${ZONE[zone].noun}</span>`;
      els.set(id, el);
    }
    el.classList.toggle("big", !!big);
    return el;
  }
  /* Put the column's children in order, moving only what is out of place (a moved element loses focus) */
  function fill(col, list) {
    [...col.children].forEach((c) => { if (!list.includes(c)) c.remove(); });
    list.forEach((el, i) => { if (col.children[i] !== el) col.insertBefore(el, col.children[i] || null); });
  }
  function setNode(el, { state, name, meta, p = null, listing = false, badge = "", label }) {
    el.dataset.state = state;
    el.querySelector(".nm").textContent = name;
    const mt = el.querySelector(".mt"); mt.textContent = meta; mt.title = meta;
    const bar = el.querySelector(".bar");
    bar.classList.toggle("listing", listing);
    if (p !== null) bar.querySelector("i").style.setProperty("--p", p);
    const b = el.querySelector(".badge");
    b.className = "badge" + (badge === "spin" ? " spin" : badge === "stop" ? " stop" : "");
    b.innerHTML = badge === "ok" ? CHECK : badge === "spin" ? " " : badge === "stop" ? "!" : "";
    el.querySelector(".tile").setAttribute("aria-label", label || `${name}: ${meta}`);
    return el;
  }
  function setHub(el, { state, name, meta, glyph, p = null, pill = "", pillAlert = false, trigger = false, cta = "" }) {
    el.dataset.state = state; el.dataset.trigger = trigger ? "yes" : "no"; el.dataset.yellow = trigger === "yellow" ? "yes" : "no";
    el.querySelector(".cta").textContent = cta;
    el.querySelector(".hub-tile").title = `${name}: ${meta}`;
    const g = el.querySelector("svg.glyph"); if (g.dataset.g !== glyph) { g.innerHTML = HUB[glyph]; g.dataset.g = glyph; }
    el.querySelector(".hub-ring .p").style.strokeDashoffset = String(439.8 * (1 - (p ?? 0)));
    const pl = el.querySelector(".pill"); pl.textContent = pill; pl.classList.toggle("alert", pillAlert);
    el.querySelector(".hub-tile").setAttribute("aria-label", `${name}: ${meta}${trigger ? ". " + cta : ""}`);
  }

  /* ---------- Render ---------- */
  const nf = (n) => n.toLocaleString("en-US");
  function render() {
    const ready = kReady(), building = S.k.building, built = S.k.built, planReady = pReady();
    const action = built ? "Update graph" : "Build graph", planAction = S.plan.on ? "Replan" : "Build plan";
    /* Yellow is for the two decisions a person makes: build the graph, then build (or replan) the plan. One at a time. */
    const kTrig = ready ? "yellow" : false, pTrig = planReady ? (ready ? true : "yellow") : false;

    /* Knowledge: sources and the hub */
    const srcEls = S.sources.map((s) => {
      const el = nodeEl(s.id, s.kind);
      const kindName = KINDS[s.kind].name, label = `${kindName} ${s.name}`;
      if (s.status === "listing") setNode(el, { state: "busy", name: s.name, meta: "Listing files…", listing: true, badge: "spin", label: `${label}: listing files` });
      else if (s.status === "importing") setNode(el, { state: "busy", name: s.name, meta: `${s.done} of ${s.files} files`, p: s.done / s.files, badge: "spin", label: `${label}: ${s.done} of ${s.files} files` });
      else if (s.status === "stopped") setNode(el, { state: "wait", name: s.name, meta: "Stopped · reconnect", badge: "stop", label: `${label}: stopped, the GitHub connection lost access` });
      else setNode(el, { state: "on", name: s.name, meta: `${s.files} files · ${kindName}`, badge: "ok", label: `${label}: ${s.files} files` });
      return el;
    });
    fill(cols.sources, [...srcEls, addEl("knowledge", S.sources.length === 0)]);

    const kh = hubEl("khub");
    let km, ks;
    if (!S.sources.length) { ks = "off"; km = "Add a source to start"; }
    else if (importing().length) { ks = "wait"; km = `Importing ${importing().length} of ${S.sources.length} sources…`; }
    else if (building) { ks = "busy"; const i = Math.min(10, Math.floor(S.k.p * 11)); km = `Step ${i + 1} of 11 · ${BUILD_STEPS[i]}`; }
    else if (ready) { ks = "ready"; km = built ? "New sources ready · update when you are" : `${filesOf(S.sources)} files ready to build`; }
    else { ks = "on"; km = `${nf(S.k.items)} items · built just now`; }
    setHub(kh, { state: ks, name: "Graph", meta: km, glyph: canvas.dataset.glyph, p: building ? S.k.p : null, trigger: kTrig, cta: action });
    fill(cols.khub, [kh]);

    /* Gap */
    const gh = hubEl("ghub");
    if (!built) setHub(gh, { state: "off", name: "Gaps", meta: "Waits for the graph", glyph: "gap" });
    else setHub(gh, { state: "on", name: "Gaps", meta: `${S.gaps.total} found · ${S.gaps.answered} answered`, glyph: "gap", pill: S.gaps.critical ? `${S.gaps.critical} critical` : "", pillAlert: true });
    const chEls = S.channels.map((c) => {
      const el = nodeEl(c.id, c.kind);
      if (!S.gaps.on) setNode(el, { state: "wait", name: c.name, meta: "Waits for gaps", label: `${KINDS[c.kind].name} ${c.name}: waits for gaps` });
      else setNode(el, { state: "on", name: c.name, meta: `${S.gaps.asked} asked · ${S.gaps.answered} answered`, badge: "ok", label: `${KINDS[c.kind].name} ${c.name}: ${S.gaps.asked} asked, ${S.gaps.answered} answered` });
      return el;
    });
    fill(cols.ghub, [gh]);
    const mcpG = S.gaps.on ? setNode(nodeEl("mcp-g", "mcp"), { state: "on", name: "MCP", meta: MCP.gap.meta, label: "MCP, added automatically: how to connect a coding agent" }) : null;
    fill(cols.channels, [...chEls, mcpG, addEl("gap", true)].filter(Boolean)); // a row under the hub: the plus is a full-size tile

    /* Plan */
    const ph = hubEl("phub");
    if (!built) setHub(ph, { state: "off", name: "Plan", meta: "Waits for the graph", glyph: "plan" });
    else if (S.plan.building) setHub(ph, { state: "busy", name: "Plan", meta: "Planning stories and tasks…", glyph: "plan", p: S.plan.p });
    else if (!S.plan.on && !gapsDone()) setHub(ph, { state: "wait", name: "Plan", meta: "Waits while the gaps are found", glyph: "plan" });
    else if (!S.plan.on) setHub(ph, { state: "ready", name: "Plan", meta: S.gaps.critical ? `Gaps found · ${S.gaps.critical} critical still open · ready to plan` : "Gaps found · ready to plan", glyph: "plan", trigger: pTrig, cta: "Build plan" });
    else if (planReady) setHub(ph, { state: "ready", name: "Plan", meta: "New information · replan to refine the stories", glyph: "plan", trigger: pTrig, cta: "Replan" });
    else setHub(ph, { state: "on", name: "Plan", meta: `${S.plan.stories} stories · ${S.plan.tasks} tasks`, glyph: "plan" });
    const dEls = S.dests.map((d) => {
      const el = nodeEl(d.id, d.kind);
      if (!S.plan.on) setNode(el, { state: "wait", name: d.name, meta: "Waits for the plan" });
      else setNode(el, { state: "on", name: d.name, meta: S.plan.syncT > 0 ? "Syncing…" : "Synced just now", badge: S.plan.syncT > 0 ? "spin" : "ok" });
      return el;
    });
    fill(cols.phub, [ph]);
    const mcpP = S.plan.on ? setNode(nodeEl("mcp-p", "mcp"), { state: "on", name: "MCP", meta: MCP.plan.meta, label: "MCP, added automatically: how to connect a coding agent" }) : null;
    fill(cols.dests, [...dEls, mcpP, addEl("plan", dEls.length === 0 && !mcpP)].filter(Boolean));

    document.querySelectorAll(".zone").forEach((z) => { z.dataset.live = String(z.dataset.zone === "knowledge" ? S.sources.length > 0 : z.dataset.zone === "gap" ? S.gaps.on : S.plan.on); });

    /* Alternatives B and C */
    const seam = $("seam"), seamP = $("seam-p");
    seam.classList.toggle("show", ready); seam.classList.toggle("yellow", kTrig === "yellow");
    seam.dataset.label = action; seam.setAttribute("aria-label", action);
    seamP.classList.toggle("show", planReady); seamP.classList.toggle("yellow", pTrig === "yellow");
    seamP.dataset.label = planAction; seamP.setAttribute("aria-label", planAction);
    const docs = filesOf(S.sources);
    $("dock-say").textContent = !S.sources.length ? "Add a source to start." : importing().length ? `Importing ${importing().length} of ${S.sources.length} sources…`
      : building ? `Building · ${Math.round(S.k.p * 100)}%` : ready ? `${docs} files from ${S.sources.length} sources are ready.`
      : planReady ? (S.plan.on ? "New information since the plan. Replan to refine it." : "The gaps are found. The plan can be built.") : S.plan.building ? `Planning · ${Math.round(S.plan.p * 100)}%`
      : S.plan.on ? `The plan is in Jira · ${S.plan.stories} stories, ${S.plan.tasks} tasks.` : `The graph is up to date · ${nf(S.k.items)} items.`;
    const db = $("dock-btn"), live = ready || planReady;
    db.textContent = ready ? action : planReady ? planAction : action; db.disabled = !live;
    db.classList.toggle("yellow", live); db.classList.toggle("ghost", !live);

    $("sc-name").textContent = `${sceneIndex() + 1} · ${SCENES[sceneIndex()].name}`;
    requestAnimationFrame(() => { drawWires(); drawBg(); placeSeam(); });
  }


  /* ---------- Wires ---------- */
  const NS = "http://www.w3.org/2000/svg";
  const box = (el) => { const r = el.getBoundingClientRect(), c = canvas.getBoundingClientRect(); return { l: r.left - c.left, r: r.right - c.left, t: r.top - c.top, b: r.bottom - c.top, cx: (r.left + r.right) / 2 - c.left, cy: (r.top + r.bottom) / 2 - c.top }; };
  const tileOf = (id) => { const el = els.get(id); return el && el.isConnected ? el.querySelector(".tile, .hub-tile") : null; };
  function drawWires() {
    const W = canvas.clientWidth, H = canvas.clientHeight;
    wires.setAttribute("viewBox", `0 0 ${W} ${H}`);
    const out = [];
    const wire = (a, b, cls, opts = {}) => {
      /* down: from under the hub's hanging name and status, straight down to a tile below it */
      const A = tileOf(a), B = tileOf(b); if (!A || !B) return;
      const p = box(A), q = box(B);
      let d, s, e;
      if (!opts.down && q.l >= p.r - 4) {
        s = [p.r + 6, p.cy]; e = [q.l - 6, q.cy];
        const dx = Math.max(30, (e[0] - s[0]) / 2);
        d = `M${s[0]} ${s[1]} C${s[0] + dx} ${s[1]} ${e[0] - dx} ${e[1]} ${e[0]} ${e[1]}`;
      } else {
        const down = q.t > p.b; s = [p.cx, down ? p.b + 6 : p.t - 6]; e = [q.cx, down ? q.t - 6 : q.b + 6];
        const dy = (e[1] - s[1]) / 2;
        d = `M${s[0]} ${s[1]} C${s[0]} ${s[1] + dy} ${e[0]} ${e[1] - dy} ${e[0]} ${e[1]}`;
      }
      out.push(`<path class="${cls}" d="${d}"/>`);
      const off = cls.includes("w-off") ? " off" : "";
      out.push(`<circle class="end${off}" cx="${s[0]}" cy="${s[1]}" r="3.2"/><circle class="end${off}" cx="${e[0]}" cy="${e[1]}" r="3.2"/>`);
    };
    const k = S.k;
    S.sources.forEach((s) => wire(s.id, "khub", s.status === "stopped" ? "w-off" : s.status !== "ok" || k.building ? "w-flow" : "w-on"));
    wire("khub", "ghub", k.built ? (S.gaps.asked < S.gaps.target ? "w-flow" : "w-on") : "w-off");
    /* A wire moves only while data moves on it: questions going out, answers coming in, a sync running */
    S.channels.forEach((c) => wire("ghub", c.id, S.gaps.on ? (S.gaps.asked < S.gaps.target || S.gaps.flowT > 0 ? "w-flow" : "w-on") : "w-off", { down: true }));
    wire("ghub", "mcp-g", "w-on", { down: true });
    wire("phub", "mcp-p", "w-on");
    wire("ghub", "phub", S.plan.on || S.plan.building ? (S.plan.building ? "w-flow" : "w-on") : "w-off");
    S.dests.forEach((d) => wire("phub", d.id, S.plan.on ? (S.plan.syncT > 0 ? "w-flow" : "w-on") : "w-off"));
    /* With reduced motion a moving wire is solid with an arrow, so it never looks like an inactive dashed one */
    wires.innerHTML = '<defs><marker id="w-arr" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto"><path class="w-arrow" d="M0 0 10 5 0 10z"/></marker></defs>' + out.join("");
  }

  /* ---------- Background: a graph that grows as the knowledge is built ---------- */
  const rng = (a) => () => { a |= 0; a = (a + 0x6d2b79f5) | 0; let t = Math.imul(a ^ (a >>> 15), 1 | a); t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t; return ((t ^ (t >>> 14)) >>> 0) / 4294967296; };
  const BG = (() => {
    const r = rng(42), pts = [];
    for (let i = 0; i < 60; i++) pts.push({ x: 0.02 + r() * 0.96, y: 0.04 + r() * 0.92, j: r(), s: 1.2 + r() * 0.8 });
    const links = [];
    pts.forEach((p, i) => {
      pts.map((q, j) => ({ j, d: (p.x - q.x) ** 2 * 3 + (p.y - q.y) ** 2 })).filter((o) => o.j !== i).sort((a, b) => a.d - b.d).slice(0, 2)
        .forEach((o) => { if (!links.some(([a, b]) => (a === o.j && b === i))) links.push([i, o.j]); });
    });
    return { pts, links };
  })();
  /* Faint dots and hairlines, always lighter than an inactive wire. A mask keeps them off every tile, label and wire.
     During the build they light up from the Knowledge hub outward: knowledge spreading toward Gap and Plan. */
  function drawBg() {
    if (canvas.dataset.bg !== "graph") return;
    const W = canvas.clientWidth, H = canvas.clientHeight;
    bg.setAttribute("viewBox", `0 0 ${W} ${H}`);
    const hub = tileOf("khub"), c = hub ? box(hub) : { cx: W * 0.25, cy: H / 2 };
    const dist = BG.pts.map((p) => Math.hypot(p.x * W - c.cx, p.y * H - c.cy)), far = Math.max(...dist) || 1;
    const rank = BG.pts.map((p, i) => 0.85 * (dist[i] / far) + 0.15 * p.j);
    const k = S.k, reveal = k.built ? 1.01 : k.building ? k.p : 0;
    const lit = (i) => rank[i] < reveal;
    const holes = [...canvas.querySelectorAll(".tile, .hub-tile, .add, .nm, .mt, .pill, .zone-label, .seam, .dock, #wires text")]
      .filter((el) => el.getClientRects().length).map((el) => { const q = box(el); return `<rect x="${q.l - 16}" y="${q.t - 16}" width="${q.r - q.l + 32}" height="${q.b - q.t + 32}" rx="14"/>`; }).join("")
      + [...wires.querySelectorAll("path")].map((w) => `<path d="${w.getAttribute("d")}" stroke-width="24" fill="none" stroke="#000"/>`).join("");
    const lines = BG.links.filter(([a, b]) => Math.hypot((BG.pts[a].x - BG.pts[b].x) * W, (BG.pts[a].y - BG.pts[b].y) * H) <= 0.15 * W)
      .map(([a, b]) => { const p = BG.pts[a], q = BG.pts[b]; return `<line class="${lit(a) && lit(b) ? "lit" : ""}" x1="${p.x * W}" y1="${p.y * H}" x2="${q.x * W}" y2="${q.y * H}"/>`; }).join("");
    const dots = BG.pts.map((p, i) => `<circle class="${lit(i) ? "lit" : ""}" cx="${p.x * W}" cy="${p.y * H}" r="${p.s}"/>`).join("");
    bg.innerHTML = `<defs><mask id="bg-mask" maskUnits="userSpaceOnUse" x="0" y="0" width="${W}" height="${H}"><rect width="${W}" height="${H}" fill="#fff"/><g fill="#000">${holes}</g></mask></defs><g mask="url(#bg-mask)">${lines}${dots}</g>`;
  }

  /* ---------- The tick: imports, builds, questions, the plan ---------- */
  function tick() {
    let changed = false;
    S.sources.forEach((s) => {
      if (s.status === "listing") { s.listT += 1; if (s.listT > 8) { s.status = "importing"; s.done = 0; } changed = true; }
      else if (s.status === "importing") { s.done = Math.min(s.files, s.done + 1); if (s.done >= s.files) { s.status = "ok"; if (S.k.built) S.k.stale = true; } changed = true; }
    });
    if (S.k.building) {
      S.k.p = Math.min(1, S.k.p + 0.018); changed = true;
      if (S.k.p >= 1) {
        const first = !S.k.built;
        Object.assign(S.k, { building: false, built: true, stale: false, items: 1290 + filesOf(S.sources) * 2 });
        if (first) Object.assign(S.gaps, { on: true, total: 14, critical: 3, asked: 0, answered: 0, target: 9 });
        /* A rebuild keeps the gaps: new information opens new ones and closes the ones it settles (D8); the plan asks for a replan (D9) */
        else { Object.assign(S.gaps, { total: S.gaps.total + 2, answered: S.gaps.answered + 1, target: S.gaps.target + 2 }); if (S.plan.on) S.plan.stale = true; }
      }
    }
    if (S.gaps.on && S.gaps.asked < S.gaps.target) { S.gaps.asked += 1; if (S.gaps.asked === 6) { S.gaps.answered = 2; S.gaps.flowT = 12; } changed = true; }
    if (S.gaps.flowT > 0) { S.gaps.flowT -= 1; changed = true; }
    /* Jira syncs every 15 s in the demo (every few minutes in the product); the wire moves only during a sync */
    if (S.plan.on) { if (S.plan.syncT > 0) { S.plan.syncT -= 1; changed = true; } else if (--S.plan.syncIn <= 0) { S.plan.syncT = 10; S.plan.syncIn = 75; changed = true; } }
    if (S.plan.building) {
      S.plan.p = Math.min(1, S.plan.p + 0.04); changed = true;
      if (S.plan.p >= 1) { const re = S.plan.on; Object.assign(S.plan, { building: false, on: true, stale: false, stories: re ? S.plan.stories + 1 : 6, tasks: re ? S.plan.tasks + 4 : 31, syncT: 10, syncIn: 75 }); }
    }
    if (changed) render();
    play.step();
  }

  /* ---------- Actions ---------- */
  function startBuild(lvl = level) { if (!kReady()) return; level = lvl; S.k.building = true; S.k.p = 0; render(); }
  function startPlan() { if (!pReady()) return; S.plan.building = true; S.plan.p = 0; render(); }
  function answerCritical() { Object.assign(S.gaps, { critical: 0, answered: S.gaps.answered + 7, target: S.gaps.total, flowT: 15 }); if (S.plan.on) S.plan.stale = true; render(); }

  /* ---------- Dialogs ---------- */
  function openDialog(html, focusSel) {
    mdlIn.innerHTML = html;
    if (!mdl.open) mdl.showModal();
    (mdlIn.querySelector(focusSel || "[autofocus]") || mdlIn.querySelector(".mdl-x")).focus();
  }
  const head = (eyebrow, title) => `<div class="mdl-top"><div><p class="eyebrow">${esc(eyebrow)}</p><h2 id="mdl-title">${esc(title)}</h2></div><button class="mdl-x" type="button" data-close aria-label="Close">×</button></div>`;
  /* Kinds that need the project's integration first (integrations.html): [integration]. Upload and Drive need none. */
  const NEEDS = { github: ["github"], ghplan: ["github"], slack: ["slack"], jira: ["atlassian"], jiragap: ["atlassian"], confluence: ["atlassian"], jirasrc: ["atlassian"], telegram: ["telegram"], notion: ["notion"], linear: ["linear"] };
  const APP_NAME = { github: "GitHub", slack: "Slack", atlassian: "Atlassian", telegram: "Telegram", notion: "Notion", linear: "Linear" };
  /* The Integrations page's state (localStorage, shared by both pages); a fresh browser starts with GitHub set up */
  const integ = () => {
    let st = null;
    try { st = JSON.parse(localStorage.getItem("gr-integrations")); } catch (e) { /* storage blocked */ }
    return st || { github: { by: "Alex Kim", repos: ["northwind/ordering", "northwind/ordering-web", "northwind/deploy"] } };
  };
  const integrated = (kind) => { const n = NEEDS[kind]; return !n || !!integ()[n[0]]; };
  const card = (kind, disabled, note) => {
    const K = KINDS[kind], setUp = !disabled && !integrated(kind);
    const tag = disabled && K.avail === "now" ? "For " + ZONE_NAME[K.zone] : setUp ? "Set up first" : AVAIL[K.avail];
    const line = setUp ? `Connect ${APP_NAME[NEEDS[kind][0]]} in Integrations first.` : note || K.line;
    return `<button class="ccard" type="button" data-kind="${kind}" ${setUp ? 'data-setup="yes"' : ""} ${disabled ? 'aria-disabled="true"' : ""}><span class="ic">${icon(iconKind(kind))}</span><b>${esc(K.name)}</b><small>${esc(line)}</small><span class="tag">${esc(tag)}</span></button>`;
  };

  function openAdd(zone) {
    const mine = Object.keys(KINDS).filter((k) => KINDS[k].zone === zone);
    const other = Object.keys(KINDS).filter((k) => KINDS[k].zone !== zone);
    openDialog(`${head(`${ZONE[zone].n} · ${ZONE_NAME[zone]}`, ZONE[zone].title)}
      <p class="intro">${esc(ZONE[zone].intro)}</p>
      <div class="cards">${mine.map((k) => card(k, KINDS[k].avail === "next")).join("")}</div>
      <details class="cards-other"><summary>Connections for the other stages</summary><div class="cards">${other.map((k) => card(k, true, `${ZONE_NAME[KINDS[k].zone]}: ${KINDS[k].line}`)).join("")}</div></details>
      <p class="always">Always on: <b>MCP</b> for coding agents. GitHub, Atlassian and Slack are connected in <a href="integrations.html">Integrations</a>.</p>`, ".ccard:not([aria-disabled])");
    mdlIn.dataset.zone = zone;
  }

  const FORMS = {
    upload: () => `<div class="drop"><b>Drop files here</b><span class="small muted">PDF, Word, PowerPoint, Excel, Markdown, CSV · up to 50 MB each</span><button class="btn sm ghost" type="button" data-fake-pick>Choose files</button><span class="small" id="picked"></span></div>`,
    gdrive: () => `<ol class="steps-ol"><li>Share the folder with this account as a Viewer.</li><li>Paste the folder link below and test it.</li></ol>
      <div class="copy-row"><code id="sa">graphico-reader@northwind-graphico.iam.gserviceaccount.com</code><button class="btn sm ghost" type="button" data-copy="sa">Copy</button></div>
      <div class="field"><label for="f-url">Google Drive folder link</label><input id="f-url" autofocus value="https://drive.google.com/drive/folders/1Northwind-specs"></div><p class="small" id="test-note" aria-live="polite"></p>`,
    /* The project's repositories come from the GitHub integration; each one picked becomes its own node */
    github: () => {
      const on = new Set(S.sources.filter((x) => x.kind === "github").map((x) => x.name)), repos = integ().github?.repos || [];
      const free = repos.filter((r) => !on.has(r.split("/")[1]));
      return `<fieldset class="pick"><legend class="eyebrow">Repositories of this project</legend>${free.length ? free.map((r, i) => `<label class="pick-row"><input type="checkbox" data-repo="${esc(r)}" ${i === 0 ? "checked autofocus" : ""}><span>${esc(r)}</span></label>`).join("")
        : '<p class="small muted">Every repository of this project is already on the canvas.</p>'}</fieldset>
        <p class="small muted">Each repository becomes its own source. Code, docs and READMEs are read; issues and pull requests are not. To add another repository of the organization, pick it in <a href="integrations.html#setup=github">Integrations › GitHub</a>.</p>`;
    },
    slack: () => `<ol class="steps-ol"><li>In Slack, invite the app to the channel where the people who can answer talk.</li></ol>
      <div class="copy-row"><code id="sl-inv">/invite @Graphico</code><button class="btn sm ghost" type="button" data-copy="sl-inv">Copy</button></div>
      <ol class="steps-ol" start="2"><li>In the same channel, connect it to this project. One channel per project.</li></ol>
      <div class="copy-row"><code id="sl-con">/graphico connect nwo-7f3a</code><button class="btn sm ghost" type="button" data-copy="sl-con">Copy</button></div>
      <ol class="steps-ol" start="3"><li>Test it: Graphico posts a welcome message in the channel.</li></ol><p class="small" id="test-note" aria-live="polite"></p>`,
    jira: () => `<div class="field"><label for="f-url">Jira space key</label><input id="f-url" autofocus value="NWO"></div>
      <div class="field"><label for="f-epic">Epic key</label><input id="f-epic" value="NWO-1"></div>
      <p class="small muted">Leave the epic empty and Graphico creates one named after the project. Each story and sub-task is created under it and kept up to date.</p><p class="small" id="test-note" aria-live="polite"></p>`,
    /* Atlassian through the project's integration: one node per link, per epic */
    confluence: () => `<div class="field"><label for="f-url">Confluence page or space link</label><input id="f-url" autofocus value="https://northwind.atlassian.net/wiki/spaces/ORD"></div>
      <label class="pick-row"><input type="checkbox" id="f-follow" checked><span>Follow links to other pages and Jira issues</span></label><p class="small" id="test-note" aria-live="polite"></p>`,
    jirasrc: () => `<div class="field"><label for="f-url">Epic key or link</label><input id="f-url" autofocus value="NWO-12"></div>
      <p class="small muted">The epic and every issue under it are read, with their comments.</p><p class="small" id="test-note" aria-live="polite"></p>`,
    jiragap: () => `<div class="field"><label for="f-url">Jira space key</label><input id="f-url" autofocus value="NWO"></div>
      <div class="field"><label for="f-group">Sub-tasks</label><select id="f-group"><option>One per gap</option><option>Grouped by area</option></select></div>
      <p class="small muted">Graphico creates a story for the open gaps; each sub-task is a problem to solve. Answers come back from the sub-tasks.</p><p class="small" id="test-note" aria-live="polite"></p>`,
    notion: () => `<div class="field"><label for="f-url">Notion page or database link</label><input id="f-url" autofocus value="https://www.notion.so/northwind/Ordering-specs"></div>
      <p class="small muted">The page and its sub-pages are read. Share the page with the Graphico connection in Notion first.</p><p class="small" id="test-note" aria-live="polite"></p>`,
    obsidian: () => `<div class="drop"><b>Drop the vault folder or a zip of it</b><span class="small muted">Markdown notes; [[links]] between them are kept</span><button class="btn sm ghost" type="button" data-fake-pick>Choose a folder</button><span class="small" id="picked"></span></div>`,
    telegram: () => `<ol class="steps-ol"><li>In Telegram, add the bot to the group where the people who can answer talk.</li></ol>
      <div class="copy-row"><code id="tg-bot">@GraphicoBot</code><button class="btn sm ghost" type="button" data-copy="tg-bot">Copy</button></div>
      <ol class="steps-ol" start="2"><li>In the group, connect it to this project. One group per project.</li></ol>
      <div class="copy-row"><code id="tg-con">/connect nwo-7f3a</code><button class="btn sm ghost" type="button" data-copy="tg-con">Copy</button></div>
      <ol class="steps-ol" start="3"><li>Test it: Graphico posts a welcome message in the group.</li></ol><p class="small" id="test-note" aria-live="polite"></p>`,
    linear: () => `<div class="field"><label for="f-url">Linear team</label><input id="f-url" autofocus value="NWO"></div>
      <div class="field"><label for="f-epic">Project</label><input id="f-epic" value="Northwind ordering"></div>
      <p class="small muted">Leave the project empty and Graphico creates one named after this project. Stories and tasks become issues, kept up to date.</p><p class="small" id="test-note" aria-live="polite"></p>`,
    ghplan: () => `<div class="field"><label for="f-url">Repository</label><select id="f-url" autofocus>${(integ().github?.repos || []).map((r) => `<option>${esc(r)}</option>`).join("")}</select></div>
      <p class="small muted">Stories and tasks become issues on the repository's project, kept up to date.</p><p class="small" id="test-note" aria-live="polite"></p>`,
  };
  /* Kinds checked before they are added: [button, while testing, result, then] */
  const TESTS = {
    gdrive: ["Test connection", "Testing…", "Connected: Specs folder, 24 files.", "Add source"],
    slack: ["Test connection", "Posting…", () => `Posted in #northwind-specs: “Welcome to the workspace of project ${document.querySelector(".proj").firstChild.textContent}.” If it arrived, add the channel.`, "Add channel"],
    jira: ["Check access", "Checking…", "Jira space Northwind ordering · Epic NWO-1: Northwind ordering.", "Add destination"],
    confluence: ["Check link", "Checking…", "Space Ordering · 42 pages, 12 linked Jira issues.", "Add source"],
    jirasrc: ["Check epic", "Checking…", "NWO-12 · Checkout redesign · 18 issues.", "Add source"],
    jiragap: ["Check access", "Checking…", "Jira space Northwind ordering. The story “Open questions · Northwind ordering” is created when gaps are found.", "Add channel"],
    ghplan: ["Check access", "Checking…", () => `${mdlIn.querySelector("#f-url").value} · project “Northwind plan”.`, "Add destination"],
    notion: ["Check link", "Checking…", "Ordering specs · 27 pages.", "Add source"],
    telegram: ["Test connection", "Posting…", () => `Posted in Northwind specs: “Welcome to the workspace of project ${document.querySelector(".proj").firstChild.textContent}.” If it arrived, add the group.`, "Add channel"],
    linear: ["Check access", "Checking…", () => `Team ${mdlIn.querySelector("#f-url").value} · project “${mdlIn.querySelector("#f-epic").value || "Northwind ordering"}”.`, "Add destination"],
  };
  function openForm(kind) {
    const K = KINDS[kind], zone = K.zone;
    const verb = TESTS[kind] ? TESTS[kind][0] : kind === "upload" ? "Add files" : kind === "obsidian" ? "Add vault" : kind === "github" ? "Add repositories" : `Add ${ZONE[zone].noun}`;
    const title = { gdrive: "Connect a Google Drive folder", github: "Add repositories", slack: "Connect a Slack channel", jira: "Send the plan to Jira",
      confluence: "Add a Confluence link", jirasrc: "Add a Jira epic", jiragap: "Send gaps to Jira", ghplan: "Send the plan to GitHub Projects",
      notion: "Add a Notion page", obsidian: "Add an Obsidian vault", telegram: "Connect a Telegram group", linear: "Send the plan to Linear" }[kind] || `Connect ${K.name}`;
    openDialog(`${head(`${ZONE[zone].n} · ${ZONE_NAME[zone]} · ${K.name}`, title)}
      <div class="form">${FORMS[kind]()}</div>
      <div class="mdl-foot"><button class="linkbtn" type="button" data-back="${zone}">Back</button><button class="btn" type="button" data-connect="${kind}">${verb}</button></div>`, "[autofocus], [data-fake-pick]");
  }
  function connect(kind, btn) {
    const v = (mdlIn.querySelector("#f-url") || {}).value || "";
    const T = TESTS[kind];
    if (T && btn.dataset.tested !== "yes") {
      btn.disabled = true; btn.textContent = T[1];
      setTimeout(() => { btn.disabled = false; btn.dataset.tested = "yes"; btn.textContent = T[3]; $("test-note").innerHTML = `<span class="note-ok">${esc(typeof T[2] === "function" ? T[2]() : T[2])}</span>`; }, 900);
      return;
    }
    if (kind === "github") {
      const picked = [...mdlIn.querySelectorAll("[data-repo]:checked")].map((x) => x.dataset.repo);
      if (!picked.length) return;
      picked.forEach((r, i) => S.sources.push(src("github", r.split("/")[1], [18, 11, 7][i % 3], "listing")));
      mdl.close(); render(); return;
    }
    const epic = (mdlIn.querySelector("#f-epic") || {}).value || "";
    const name = { upload: "Kick-off pack", gdrive: "Specs folder", slack: "#northwind-specs", confluence: "Ordering space",
      jira: `Jira · ${epic.trim() || v.trim()}`, jirasrc: v.trim().split("/").pop() || "Jira epic", jiragap: `Jira · ${v.trim()}`, ghplan: `${v.split("/").pop()} project`,
      notion: "Ordering specs", obsidian: "Team vault", telegram: "Northwind specs", linear: `Linear · ${v.trim()}` }[kind] || v || KINDS[kind].name;
    const files = { upload: 6, gdrive: 24, confluence: 42, jirasrc: 18, notion: 27, obsidian: 64 }[kind] || 0;
    const z = KINDS[kind].zone;
    if (kind === "slack" || kind === "telegram") S.channels = S.channels.filter((c) => c.kind !== kind); // one channel or group per project: connecting another moves it
    if (z === "knowledge") S.sources.push(src(kind, name, files, "importing"));
    else (z === "gap" ? S.channels : S.dests).push(src(kind, name, 0, "ok"));
    mdl.close(); render();
  }

  function openBuild() {
    if (!kReady()) return;
    const docs = filesOf(S.sources), upd = S.k.built;
    const lv = () => LEVELS.map((l, i) => `<button type="button" data-level="${i + 1}" aria-pressed="${i + 1 === level}">${l}</button>`).join("");
    openDialog(`${head("01 · Graph", upd ? "Update the graph" : "Build the graph")}
      <p class="intro">Graphico reads ${docs} files from ${S.sources.length} sources and builds one graph. ${upd ? "Open gaps stay open; new information can open new ones, and closes the ones it settles." : "The gaps follow on their own."}</p>
      <div class="stack-8"><p class="eyebrow">Reasoning level</p><div class="seg levels" role="group" aria-label="Reasoning level">${lv()}</div><p class="small muted" id="est">Takes ${estimate(docs, level)}. Higher levels read more carefully and take longer.</p></div>
      <div class="mdl-foot"><button class="linkbtn" type="button" data-close>Cancel</button><button class="btn yellow" type="button" data-start autofocus>${upd ? "Update" : "Build"}</button></div>`);
  }
  function openPlanBuild() {
    if (!pReady()) return;
    const dest = S.dests[0], re = S.plan.on;
    openDialog(`${head("03 · Plan", re ? "Replan" : "Build the plan")}
      <p class="intro">${re ? "There is new information since the plan was made. Graphico refines the plan the way a backlog refinement would: it adds the stories the new information needs, changes the ones it touches and cancels the ones it makes obsolete. The plan is never made again from scratch; claimed and finished work stays."
        : `Graphico turns the graph into areas, stories and tasks, with the order they must be built in.${dest ? ` They go to ${esc(dest.name)} and are kept up to date.` : " Add a destination to send them to Jira."}`}</p>
      <p class="small muted">${S.gaps.total - S.gaps.answered} gaps are still open${S.gaps.critical ? `, ${S.gaps.critical} of them critical. The work they block waits until they are answered` : ""}.</p>
      <div class="mdl-foot"><button class="linkbtn" type="button" data-close>Cancel</button><button class="btn yellow" type="button" data-plan autofocus>${re ? "Replan" : "Build plan"}</button></div>`);
  }
  function openMcp(zone) {
    const m = MCP[zone];
    openDialog(`${head(`${ZONE[zone].n} · ${ZONE_NAME[zone]} · Added automatically`, "Connect a coding agent over MCP")}
      <p class="intro">${esc(m.say)} Graphico added this when the ${zone === "gap" ? "gaps were found" : "plan was built"}; there is nothing to set up.</p>
      <div class="stack-8"><p class="eyebrow">Claude Code</p><div class="copy-row"><code id="cmd">claude mcp add --transport http graphico https://&lt;your-graphico&gt;/mcp</code><button class="btn sm ghost" type="button" data-copy="cmd">Copy</button></div>
      <p class="small muted">Then run <code>/mcp</code>, select graphico and sign in. Start with <code>${m.prompt}</code>. Cursor, Codex and other clients: add the same URL as a remote MCP server.</p></div>
      <div class="stack-8"><p class="eyebrow">Tools for this stage</p><div class="tools">${m.tools.map((t) => `<code>${t}</code>`).join("")}</div></div>
      <div class="mdl-foot"><span></span><button class="btn ghost" type="button" data-close autofocus>Done</button></div>`);
  }
  function openGaps() {
    if (!S.k.built) return;
    const c = S.gaps.critical;
    openDialog(`${head("02 · Gap", c ? `${c} critical gaps` : "Gaps")}
      <p class="intro">${c ? "These block the work they are about until they are answered. Each one is in Slack with its quote and options; anyone can also answer from a coding agent." : `${S.gaps.total} gaps found, ${S.gaps.answered} answered. None of the open ones blocks the plan.`}</p>
      ${c ? `<ul class="stack-8"><li><b>Refunds for paid orders</b> <span class="small muted">· contradiction · Ordering spec 4.2 and Payments 2.1</span></li><li><b>Approval limit for managers</b> <span class="small muted">· missing · Ordering spec 4.3</span></li><li><b>Reporting API owner</b> <span class="small muted">· missing · Admin spec, Reporting</span></li></ul>` : ""}
      <div class="mdl-foot"><span class="small muted">${c ? "Prototype: pretend the answers came back." : ""}</span>${c ? '<button class="btn" type="button" data-answer autofocus>Answers arrive</button>' : '<button class="btn ghost" type="button" data-close autofocus>Done</button>'}</div>`);
  }
  function openNode(id) {
    const all = [...S.sources, ...S.channels, ...S.dests], n = all.find((x) => x.id === id); if (!n) return;
    const K = KINDS[n.kind], app = NEEDS[n.kind]?.[0], by = app && integ()[app]?.by;
    const via = app ? ` Through ${APP_NAME[app]}, connected by ${esc(by || "Alex Kim")}.` : "";
    const say = n.status === "stopped" ? `Stopped. The ${APP_NAME[app]} account of ${esc(by || "Alex Kim")}, which this project's integration runs on, lost access to this repository, so Graphico stopped reading it. What was imported stays in the graph. Anyone on the project can reconnect ${APP_NAME[app]}.`
      : n.status === "ok" ? (K.zone === "knowledge" ? `${n.files} files, imported when it was added. Add it again to read a newer version.${via}` : `Connected.${via}`) : "Importing…";
    openDialog(`${head(`${ZONE[K.zone].n} · ${ZONE_NAME[K.zone]} · ${K.name}`, n.name)}
      <p class="intro">${say}</p>
      <div class="mdl-foot"><button class="btn sm ghost" type="button" data-remove="${id}">Remove</button>${n.status === "stopped" ? `<a class="btn" href="integrations.html#setup=${app}" autofocus>Reconnect ${APP_NAME[app]}</a>` : '<button class="btn ghost" type="button" data-close autofocus>Done</button>'}</div>`);
  }

  mdl.addEventListener("click", (e) => {
    if (e.target === mdl) { mdl.close(); return; }
    const t = e.target.closest("button"); if (!t) return;
    if (t.matches("[data-close]")) mdl.close();
    else if (t.dataset.copy) { const txt = $(t.dataset.copy).textContent.trim(); const done = () => { t.textContent = "Copied"; setTimeout(() => { t.textContent = "Copy"; }, 1600); }; navigator.clipboard?.writeText(txt).then(done, () => {}); }
    else if (t.matches(".ccard[data-setup]")) location.href = `integrations.html#setup=${NEEDS[t.dataset.kind][0]}&back=${t.dataset.kind}`; // the app's account is not connected yet
    else if (t.matches(".ccard") && t.getAttribute("aria-disabled") !== "true") openForm(t.dataset.kind);
    else if (t.dataset.back) openAdd(t.dataset.back);
    else if (t.dataset.connect) connect(t.dataset.connect, t);
    else if (t.matches("[data-fake-pick]")) { $("picked").textContent = "6 files picked: Kick-off.pptx, Scope.docx, Ordering spec.pdf and 3 more."; }
    else if (t.dataset.level) { level = Number(t.dataset.level); mdlIn.querySelectorAll("[data-level]").forEach((b) => b.setAttribute("aria-pressed", String(b === t))); $("est").textContent = `Takes ${estimate(filesOf(S.sources), level)}. Higher levels read more carefully and take longer.`; }
    else if (t.matches("[data-start]")) { mdl.close(); startBuild(); }
    else if (t.matches("[data-answer]")) { mdl.close(); answerCritical(); }
    else if (t.matches("[data-plan]")) { mdl.close(); startPlan(); }
    else if (t.dataset.remove) { const id = t.dataset.remove; ["sources", "channels", "dests"].forEach((k) => { S[k] = S[k].filter((x) => x.id !== id); }); els.delete(id); mdl.close(); render(); }
  });

  /* ---------- Canvas clicks ---------- */
  canvas.addEventListener("click", (e) => {
    const add = e.target.closest("[data-add]"); if (add) { openAdd(add.dataset.add); return; }
    if (e.target.closest("#seam")) { openBuild(); return; }
    if (e.target.closest("#seam-p")) { openPlanBuild(); return; }
    const hub = e.target.closest(".hub"); if (hub) { ({ khub: openBuild, ghub: openGaps, phub: openPlanBuild })[hub.dataset.id](); return; }
    const node = e.target.closest(".node"); if (!node) return;
    const id = node.dataset.id;
    if (id === "mcp-g" || id === "mcp-p") openMcp(id === "mcp-g" ? "gap" : "plan"); else openNode(id);
  });
  const lvlSel = $("dock-level");
  lvlSel.innerHTML = LEVELS.map((l, i) => `<option value="${i + 1}" ${i + 1 === level ? "selected" : ""}>${l} reasoning</option>`).join("");
  lvlSel.addEventListener("change", () => { level = Number(lvlSel.value); });
  $("dock-btn").addEventListener("click", () => { if (kReady()) startBuild(Number(lvlSel.value)); else startPlan(); });
  $("col-sources").addEventListener("scroll", () => drawWires(), { passive: true });

  /* ---------- Prototype controls ---------- */
  const go = (i) => { play.stop(); els.forEach((el, id) => { if (/^n\d/.test(id)) els.delete(id); }); SCENES[Math.max(0, Math.min(SCENES.length - 1, i))].set(); render(); };
  $("sc-prev").addEventListener("click", () => go(sceneIndex() - 1));
  $("sc-next").addEventListener("click", () => go(sceneIndex() + 1));
  /* First run: a person with no project gets the "Create your first project" window over an empty canvas (members.js) */
  $("sc-first").addEventListener("click", () => { go(0); window.dispatchEvent(new CustomEvent("gr:first-run")); });
  window.addEventListener("gr:new-project", () => go(0));
  document.querySelectorAll("[data-alt]").forEach((g) => g.addEventListener("click", (e) => {
    const b = e.target.closest("button[data-v]"); if (!b) return;
    g.querySelectorAll("button").forEach((x) => x.setAttribute("aria-pressed", String(x === b)));
    canvas.dataset[g.dataset.alt] = b.dataset.v;
    render();
  }));
  /* Play all: a person adds sources, builds the knowledge, builds the plan; everything else moves by itself */
  const play = (() => {
    let on = false, wait = 0, stage = 0;
    const btn = $("sc-play");
    const stop = () => { on = false; btn.textContent = "Play all"; };
    btn.addEventListener("click", () => { if (on) { stop(); return; } go(0); on = true; stage = 0; wait = 8; btn.textContent = "Stop"; });
    return {
      stop,
      step() {
        if (!on || --wait > 0) return;
        if (stage === 0) { SCENES[1].set(); render(); stage = 1; wait = 1; }
        else if (stage === 1) { if (kReady()) { stage = 2; wait = 12; } else wait = 1; }
        else if (stage === 2) { startBuild(); stage = 3; wait = 1; }
        else if (stage === 3) { if (pReady()) { stage = 4; wait = 14; } else wait = 1; }
        else if (stage === 4) { startPlan(); stage = 5; wait = 1; }
        else if (stage === 5 && S.plan.on) stop();
        else wait = 1;
      },
    };
  })();

  /* ---------- Start ---------- */
  let lastW = 0, lastH = 0;
  new ResizeObserver(() => { const w = canvas.clientWidth, h = canvas.clientHeight; if (w !== lastW || h !== lastH) { lastW = w; lastH = h; drawWires(); drawBg(); placeSeam(); } }).observe(canvas);
  /* Each gate sits on the line before the stage it starts: Knowledge|Gap for the knowledge, Gap|Plan for the plan */
  function placeSeam() {
    [["seam", "gap", "khub"], ["seam-p", "plan", "phub"]].forEach(([id, zone, hubId]) => {
      const z = document.querySelector(`.zone[data-zone="${zone}"]`), hub = tileOf(hubId); if (!z || !hub) return;
      const zb = box(z), hb = box(hub), wide = zb.t < hb.b, el = $(id);
      el.style.left = (wide ? zb.l : zb.cx) + "px";
      el.style.top = (wide ? hb.cy : zb.t) + "px";
    });
  }
  if (document.fonts && document.fonts.ready) document.fonts.ready.then(() => { drawWires(); placeSeam(); });
  SCENES[2].set();
  render();
  /* index.html#add=slack: back from Integrations after setting the app up; finish adding the connection */
  const back = new URLSearchParams(location.hash.slice(1)).get("add");
  if (back && KINDS[back] && integrated(back)) { history.replaceState(null, "", location.pathname); openForm(back); }
  setInterval(tick, 200);
})();
