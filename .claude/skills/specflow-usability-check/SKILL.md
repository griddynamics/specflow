---
name: specflow-usability-check
description: Assess how SpecFlow can be applied to a potential customer based on their specs, requirements, screenshots, and descriptions. Produces a sliding-scale fit report — always finds an angle of use, lists enablers and feature requests where needed, and only declares rejection when truly nothing fits.
argument-hint: "<path to customer docs folder, file, or screenshot>"
---

# SpecFlow Usability Check

You are assessing how **SpecFlow** can be applied to a potential customer's situation. The materials (specs, requirements, screenshots, text) live at the path the user provided. The reference below is the static knowledge about SpecFlow.

**Stakes**: this assessment feeds presales. A false rejection costs a real project and real revenue. **Default posture: find a way to use SpecFlow.** Loaded with upfront enablers, feature requests we can build quickly, and creative reframing — all acceptable. Reject only when the situation is **truly apocalyptic** (defined below).

## Input

The user provided: `$ARGUMENTS`

If empty: "Provide the path to the customer's documents folder (specs, requirements, screenshots, descriptions)."

If the path does not exist or is empty, stop and tell the user.

---

## Step 0 — Build specification_index.md

Before any analysis, create `specification_index.md` inside the customer docs path (or current working directory if read-only). This file is a fully self-contained index so subsequent agents can work from it without re-ingesting raw files.

Recursively walk the path. For each file, write a section using this template:

```
## <relative-path>
**Type**: <text|markdown|pdf|docx|image|code|config|other>
**Size**: <approximate>

<extracted content — format depends on type, see below>
```

**Per-type extraction rules:**

- **Markdown / plain text**: Include the full text if under ~300 lines; otherwise include a paragraph summary plus every heading and any list items that look like requirements, constraints, or decisions.
- **DOCX**: Extract all paragraphs and headings. Include them verbatim (cleaned of markup). Summarise only if the doc exceeds ~500 lines.
- **PDF**: Use the `pages` parameter for large files. Extract all readable text — include it nearly verbatim, cleaned of headers/footers/page numbers. Do **not** reduce to a summary; downstream agents must be able to skip re-reading the PDF. Flag any pages that appear to be pure images with "[page N: image-only — see visual description below]" and follow with a visual description.
- **Images / screenshots**: Read visually. Write a dense description: layout, all visible text (verbatim where legible), UI elements, colours, labels, form fields, menu items, error messages, architectural diagrams (nodes + edges + labels), data flows, annotations. The goal is that a reader of the index has no need to open the image. Be thorough, not terse.
- **Code / config**: Include the full file if under ~150 lines; otherwise a summary of its purpose, key symbols (classes, functions, env vars, endpoints), and any notable constraints or integration points.
- **Secret-pattern files** (`.env`, files containing `key`, `token`, `secret`, `password`, `credential` in their name): **do not read**. Add a section: `**SKIPPED — possible credential file. Listed for user review.**`

After writing all sections, append a one-paragraph **Overview** at the top of the file summarising: how many files were indexed, what types dominate, and any credential files that were skipped.

Write `specification_index.md` now, then continue to Step 1.

---

## Step 1 — Ingest customer materials

Read `specification_index.md` (just created) as the primary source. You do **not** need to re-read the raw files unless a specific detail is missing from the index.

If materials are thin (fewer than 3 documents or no technical detail), note this explicitly in the Customer Snapshot as **"Evidence quality: low — assessment confidence is reduced; recommend follow-up questions before presales call."**

Build an internal picture: domain, what they're building, stack, integration surface, testing maturity, constraints (compliance, on-prem, gateways, regional), scope (single feature vs. multi-repo vs. monolithic brownfield).

Gaps and contradictions are normal — they become questions in the report, not reasons to reject.

---

## Step 2 — Evaluate against the SpecFlow reference

For every section of the **Reference** below, record:

- **Signal** in the customer materials (quote/paraphrase + filename).
- **Verdict on a sliding scale**:
  - 🟢 **Direct fit** — works out of the box.
  - 🟡 **Fit with enablers** — works once we add X (LLM gateway, MCP gateway, scoped data agreement, on-prem variant in roadmap, etc.).
  - 🟠 **Fit with reframing** — works once we **carve a subset** (divide-and-conquer; see Creative-reframing playbook).
  - 🔵 **Fit with feature request** — works once SpecFlow adds Y. Note: we can build features quickly. Always specify what the feature would look like and rough effort class (small / medium / pivot).
  - ❓ **Unknown** — needs a customer answer; phrase the exact question.
  - 🔴 **Apocalyptic blocker** — only the situations listed in **Apocalyptic-rejection criteria** below.

**Rule of thumb**: if you find yourself reaching for 🔴, re-read the Creative-reframing playbook first. 🔴 is rare.

---

## Step 3 — Creative-reframing playbook (MANDATORY before any rejection)

Run **every** apparent blocker through these reframing lenses. Document which lens applied and how.

### Lens 1 — Divide and conquer

A "too big / too legacy / too entangled" project is almost never a blocker. Worked example:

> **Apparent blocker**: brownfield project, tens of millions of lines, hundreds of integrations, mix of legacy + cloud + 3rd-party.
> **Reframe**: identify subsystems that can be **compartmentalized** behind a stable contract — an API, a queue, a schema, a feature flag. Each compartment becomes a SpecFlow target where agents can test the majority of the code against real environments. SpecFlow does not need to swallow the whole monolith; it needs a clean seam.
> **Selling point to customer**: SpecFlow's parallel-variant approach is especially valuable on legacy because the variance across variants surfaces hidden assumptions in the legacy contract.

### Lens 2 — Carve a vertical slice

The customer's roadmap rarely needs SpecFlow for *everything*. Find the **most isolatable upcoming feature, service, or migration step**. Even a single new service alongside a legacy estate is a textbook SpecFlow target.

### Lens 3 — Modernization-by-strangler

If they're modernizing legacy: SpecFlow is excellent at generating the new-stack equivalent of an old-stack module, then operating behind a strangler facade. Defined contracts + tests = full SpecFlow capability on the new side.

### Lens 4 — Spec-first sandbox (or real test env)

If the customer has a **test/staging environment with the real integrations wired up**, point SpecFlow at it directly — that's the preferred path. Only when no such env is reachable from our sandbox do we stand up **mock-but-realistic** integration stubs as presales scope. Either way, SpecFlow runs full E2E and the customer gets variants + P10Y signal early. Real-prod integration cutover happens in production CI later.

You can also **skip deploy + E2E entirely** and use SpecFlow as a pure code-generation tool — variants are produced, P10Y scores still apply (computed from code, not runtime), customer's existing pipeline handles deploy/test. This is a legitimate lower-friction entry point for customers whose test envs are unreachable and who don't want to invest in stubs upfront.

### Lens 5 — Greenfield-adjacent

Even mostly-brownfield estates have **adjacent greenfield** work: new dashboards, new BFFs, new mobile clients, new internal tools, new admin panels. Those are pure SpecFlow plays. Surface them.

### Lens 6 — Spec-completeness pre-engagement

If specs are sparse/contradictory, that is **not** a rejection — that is the **first phase** of SpecFlow's value: the interactive spec analyzer plus the variance signal from 3 parallel variants tells the customer where their spec is incomplete. Sell that as a discovery deliverable.

### Lens 7 — Enabler stacking

If they need an LLM gateway, MCP gateway, regional model routing, customer-billed tokens, scoped data agreements, audit logging, SBOM, etc.: **stack them as presales enablers.** SpecFlow is aux tooling on top of a GD engagement — enablers are billable scope, not deal-breakers.

### Lens 8 — Feature request to SpecFlow

If a real gap exists in SpecFlow today, name it and estimate it. Examples of plausible quick additions:

- Per-tenant data isolation guarantees beyond current Evergreen tenancy.
- Self-hosted artifact storage (no GitHub.com push).
- Pluggable scoring model that replaces P10Y for customers who refuse the dependency.
- Egress allow-listing for telemetry (turn PostHog off entirely).
- BYO-LLM-endpoint adapter behind OpenRouter.

If the gap is bigger (full on-prem control plane, air-gapped operation), name it as a **pivot signal** — flag it for product, do not pretend it ships today.

---

## Step 4 — Apocalyptic-rejection criteria (the only valid 🔴 outcomes)

Reject **only** if **all** reframing lenses fail and at least one of these is true:

1. **Hard legal block on source code leaving customer perimeter**, with no path to (a) on-prem SpecFlow pivot, (b) carved subset on synthetic/non-sensitive code, or (c) spec-first sandbox.
2. **No code generation needed at all** — the engagement is pure consulting / advisory / process work with no software deliverable.
3. **Customer explicitly bans AI-assisted code generation** from any vendor under any conditions, with no override path through gateways or audit controls.
4. **Engagement is single-file or single-bug-class small**, the customer has stated in writing that there is no roadmap and no multi-repo scope, **and** the presales team has confirmed there is no adjacent work to surface — local Claude Code / Cursor is genuinely the right tool and SpecFlow would be overkill that damages credibility to recommend. Do **not** apply this criterion to small initial PoCs or foot-in-the-door engagements with visible expansion potential.

Anything else is **not** apocalyptic. Anything else gets a sliding-scale fit with enablers, reframing, or feature requests.

---

## Step 5 — Apply the calibration spectrum

Replace any binary tree with this spectrum. Place the customer on it.

```
🟢 Direct fit
   └── Full-stack greenfield / well-specced rewrite / clean integrations
🟡 Fit with enablers (presales scope additions)
   └── + LLM gateway / MCP gateway / scoped data agreement / customer billing
🟠 Fit with reframing (divide-and-conquer / carve a slice / strangler / sandbox)
   └── Brownfield monolith, sprawling legacy, unreachable integrations
🔵 Fit with feature request (we can build it; name effort class)
   └── BYO-LLM endpoint, egress controls, alt-scoring, harder tenancy
🟣 Pivot signal (product-level, not this deal)
   └── Air-gapped on-prem, hard regional isolation with no Evergreen path
🔴 Apocalyptic (rare; only the 4 criteria above)
```

The recommendation in the TL;DR maps to the **highest-color row the customer reaches**, framed as "Yes, here's how" unless 🔴.

---

## Step 6 — Produce the report

Write the report to `specflow-usability-check-report.md` **inside the customer docs path**. If that path is read-only, write to the current working directory and tell the user.

Use this exact structure:

```markdown
# SpecFlow Usability Check — {customer / project name if known}

## TL;DR (2 sentences)
Yes, we can use SpecFlow here by {one-line approach: direct / enablers / reframing / feature request / pivot}, because: {1–3 main reasons}.
(Only if 🔴: "We cannot use SpecFlow here, because: {which apocalyptic criterion}.")

## Recommended angle of use
{The concrete shape of the SpecFlow engagement for THIS customer. Which subsystems / slices / phases. Which lens(es) from the playbook applied. What variants we'd run. What gets deployed and E2E-tested vs. what runs spec-first sandbox.}

## Customer snapshot
- Domain:
- What they're building:
- Stack:
- Project type: greenfield / rewrite / modernization / feature work / mixed
- Scope signal: # components, services, repos; rough size (KLOC / # integrations)
- Integration surface: 3rd-party / cloud / internal
- Testing maturity:
- Constraints noted: compliance, on-prem, gateways, regional, billing

## Sliding-scale placement
- Calibration row: 🟢 / 🟡 / 🟠 / 🔵 / 🟣 / 🔴
- Why this row:

## Fit evaluation (per Reference section)
{For every reference section: signal found + verdict color + reasoning. Keep terse.}

## Enablers to add in presales (🟡)
{Concrete items: LLM gateway, MCP gateway, scoped data agreement, audit logging, customer LLM-token budget, regional routing, etc. Each with one-line scoping note.}

## Reframings applied (🟠)
{Which lens(es) from Step 3. Concrete subsystems / vertical slices identified. What we'd actually generate with SpecFlow vs. what we'd leave alone.}

## Feature requests to SpecFlow (🔵)
{Each: what it is, why this customer needs it, effort class (small / medium / pivot), whether it unlocks this deal alone or stacks with enablers.}

## Pivot signals to product (🟣)
{Product-level asks beyond this deal — e.g., SpecFlow local mode, air-gapped, alt-scoring SSOT.}

## Questions for the customer
{Every ❓ from Step 2, phrased as ready-to-send questions.}

## Risks and how we mitigate
{Honest risks. Each paired with a mitigation rather than left as a blocker.}

## Apocalyptic check
{Explicitly state: did any of the 4 apocalyptic criteria trigger? If not, say so. If yes, name which.}

## Recommended next steps
- [ ] {Pre-engagement action 1: e.g., confirm LLM gateway scoping with customer IT}
- [ ] {Question to send the customer before the demo}
- [ ] {Internal escalation if pivot signal: flag to product / Adam W}
```

Then output the **TL;DR + Recommended angle of use** directly in chat so the user can use it without opening the file.

---

# Reference — SpecFlow facts and fit questions

## What SpecFlow is

A coding orchestrator built around Anthropic Claude Code that automates generation, deployment, and testing of full-stack codebases. **Backend is a GD-owned service hosted on GCP (R&R Evergreen)** today — on-prem/air-gapped is a roadmap pivot, not current product.

**Architecture**: SpecFlow ships as a **STDIO-transport MCP server** that runs **only on the customer's own machine** inside Cursor, Claude Code, or GitHub Copilot. The local MCP zips and uploads **only three user-defined directories** — `src_dir`, `outputs_dir`, `specs_dir` — to the GD backend. No other files leave the developer's machine. The customer controls exactly what those three directories point at, so "scoped-repo mode" is built in, not a feature request.

Capabilities:
- **Spec-driven**: ingests slides, PDFs, transcripts, screenshots, Figma, scribbles. Interactively flags ambiguities and contradictions before coding.
- **Parallel variants**: up to 3 concurrent generations on different SOTA LLMs → 3 deployable codebases from one spec.
- **P10Y complexity scoring**: per-variant size/effort estimate; std-dev across variants is a spec-completeness signal.
- **Long-horizon execution**: runs for hours across multiple sessions; handles full-stack and cross-repo work short-burst tools can't.
- **Grounded** (optional): can deploy each variant to a real cloud sandbox and run E2E against real services or realistic stubs. **Deploy + E2E are not mandatory** — SpecFlow is also valid as a **pure code-generation tool**, leaving deploy/test to the customer's existing pipeline.
- **Real test envs welcome**: if the customer has a test/staging environment with the relevant integrations live, SpecFlow can target it directly — no need to stand up stubs.
- **Onboarding**: chat interface + 1-button MCP install. Rosetta-guided workflows. No team training needed.
- **Background execution**: results pulled on demand; multi-tasking friendly.
- **Model-agnostic via OpenRouter**: works from Cursor, Claude Code, and Copilot.
- **Quality gates**: during coding and at merge, step-by-step against company standards.

**Stack coverage**: the generation image ships toolchains for SpecFlow's tested profiles (mainly web full-stack). **Native mobile (iOS/Android) is technically supported but untested** — agents can install missing runtimes/toolchains if the customer's dependency manager declares them correctly. Flag this honestly to customers in mobile-heavy domains: it's a validation/onboarding effort, not a product gap.

## SpecFlow's external interaction surface

What leaves the customer's machine: the **zipped contents of the three configured directories** (`src_dir`, `outputs_dir`, `specs_dir`) sent to the GD backend over the STDIO MCP's upload path. Nothing else.

What the GD backend interacts with: **GCP (hosting), OpenRouter (USA/EU models), PostHog (telemetry; metadata only, no content), GitHub (commits — only when the deploy/commit path is used), P10Y.com (effort scoring)**.

Each of those is a **negotiation surface**, not a fixed dependency. Egress allow-listing, gateway routing, alt-scoring, code-gen-only mode (skips GitHub commit dependency), and self-hosted artifact paths are valid enabler/feature-request directions.

**P10Y is a selling point, not a liability.** Lead with it: the complexity/variance score across 3 parallel variants gives customers a defensible, data-backed effort estimate before engineering begins — something no other presales tool provides. Only raise the data-egress compliance question if the customer has already stated data-leaving-perimeter restrictions; otherwise, the conversation should start with the value, not the dependency.

## Fit questions — Compliance & integration prerequisites

1. Approved LLM usage for project delivery, including MCP-capable IDEs/coding agents? Which ones?
2. GD billing for LLM tokens, or per-team customer budget?
3. Can source code be sent to our Evergreen-hosted service? If not directly → carved subset, synthetic mirror, or on-prem pivot.
4. Can source code be scored by P10Y.com? If not → alt-scoring feature request (🔵).
5. Can we track user activity via PostHog (metadata only)? If not → egress-disable feature request (🔵, small).
6. LLM gateway / MCP gateway requirements? → 🟡 enablers, not blockers.
7. On-prem-only with no Evergreen path? → 🟣 pivot signal; check Lens 1/4 first.

## Fit questions — Scope & complexity of the backlog

1. How many components, services, repositories? (Bigger ≠ worse; it usually means more carve-able slices.)
2. Domain and application kind?
3. Greenfield, rewrite-in-different-stack, or modernization? All three are valid SpecFlow targets; modernization needs defined contracts and tests on the new side.
4. **Always ask**: where inside the estate is the cleanest seam for a carved SpecFlow engagement? (See Lens 1.)

## Fit questions — Integrations & testing

1. How many integrations, of which kinds (3rd-party / cloud / internal)?
2. Formal E2E test process we can leverage?
3. Real URLs / schemas / contracts reachable from sandbox? If not → 🟠 Spec-first sandbox (Lens 4).

## Strong-fit signals

- Full-stack work: UI + services + integrations together.
- Cross-repo or cross-app feature.
- Rewrite in different stack/language.
- Modernization with defined contracts and tests.
- Greenfield adjacent to a brownfield estate.
- Customer wants spec-completeness feedback via variant variance.

## Weak-fit-by-itself signals (do NOT auto-reject — carve / reframe / stack with enablers)

- Single-file change.
- Bug hunt or refactor that genuinely fits one context window.
- Single small feature in a single repo with no expansion roadmap.
- Brownfield with unclear specs — but this is where Lens 6 (spec-first) shines.
- Unreachable integrations — but Lens 4 (spec-first sandbox) handles it.

## Positioning rule

SpecFlow is **aux tooling** that accelerates a GD engagement. It is not the main dish. Enablers are scope additions. Feature requests are roadmap. Pivots are product conversations. Rejection is rare and reserved for the 4 apocalyptic criteria.
