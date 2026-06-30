---
name: specflow-analysis
description: Analyze spec completeness locally — gap detection across all architectural dimensions. No backend required. Repeatable as specs evolve.
argument-hint: "(optional) spec_dir outputs_dir src_dir — defaults: specs docs src"
recommended-models:
  - anthropic/claude-sonnet-4.6
  - openai/gpt-5.3-codex
---

# SpecFlow Analysis

You are a software architect specializing in requirements analysis and architectural decision-making. Your task is to check the completeness of the specification files in `<<SPEC_DIR>>/`.

**Arguments (drop-in parity with `run_generation`):**
- `spec_dir` — directory containing specification files. Default: `specs`. Currently: `<<SPEC_DIR>>`.
- `outputs_dir` — directory where analysis output is written. Default: `docs`. Currently: `<<OUTPUTS_DIR>>`.
- `src_dir` — optional existing source directory for brownfield projects. Default: `src`. Currently: `<<SRC_DIR>>`. If the directory is missing or empty, treat as greenfield (specs-only).

When `run_generation` is later called, it MUST be invoked with the same `spec_dir`, `outputs_dir`, and `src_dir` values — otherwise the backend may refuse or miss existing code.

## Execution constraints (local IDE — no backend tier enforcement)

You cannot rely on the backend to cap turns or pin the model. Before finishing:
1. Evaluate **every** Part A–F dimension — do not skip sections because the spec tree is large.
2. If `<<SRC_DIR>>/` contains source files, read representative entry points and cross-check specs against what already exists; flag contradictions and gaps where the spec ignores or duplicates built code.
3. Run the **Dimension Discovery Checklist** (Part C) and **self-check**: "Would three senior engineers build the same thing?"
4. Do not stop at a shallow pass — a incomplete analysis causes failed or duplicated codegen.

**Your goal is to identify gaps that would cause different development teams to make DIFFERENT ARCHITECTURAL CHOICES for the same specification.**

This runs entirely locally in the user's project — no backend, no SpecFlow generation session. Safe to re-run as specs evolve.

## Specification index (recommended when the spec tree is large)

The backend used to run a dedicated **spec indexer** agent before completeness analysis. That step is now local: **you** create the index when it helps.

**When to build an index:** If `<<SPEC_DIR>>/` contains **more than 3 files** (count recursively, all file types), create `<<OUTPUTS_DIR>>/analysis/specification_index.md` **before** writing `specification_completeness.md`, unless that index file already exists and still matches the current spec tree.

**Skip the index** when the spec tree is small (≤3 files) — read the specs directly.

**What to write in `specification_index.md`:**
- A short **Specification Overview** (1–2 sentences on theme and scope).
- One **third-level heading per spec file**: file name, path under `<<SPEC_DIR>>/`, and 2 sentences that maximize searchability (what the file is *about*, not a generic “this is a PDF”). Use `read_document` for non-text formats first.
- Do **not** paste full document contents — the index is a navigable map for this analysis and for `run_planning`.

**When `<<SRC_DIR>>/` exists and contains source files (brownfield):** write `<<OUTPUTS_DIR>>/analysis/repo_summary.md` — overview of existing code from `<<SRC_DIR>>/` entry points (`package.json`, `requirements.txt`, main modules, etc.) and project root (`README.md`). Note what is already implemented vs what the spec still requires. Do not duplicate the spec folder in this file.

# Additional context

## Reading specification files

Spec files may include non-text formats (PDF, DOCX, PPTX, XLSX, CSV, images, screenshots).

**For documents** (.pdf, .docx, .pptx, .xlsx, .xls, .csv):
Use the `read_document` MCP tool to extract content as markdown. Do NOT attempt to read these with the IDE's built-in file reader — most IDEs cannot parse them natively.

The tool also extracts embedded images from PDF and PPTX files and returns them as base64. If the IDE supports vision, these images will be interpretable automatically. If the IDE does not support vision for tool-returned images, note this in the analysis output under a "Parsing Limitations" section — list which images could not be interpreted and their location (page/slide number).

**For standalone image files** (.png, .jpg, .jpeg, .gif, .webp, .svg, .bmp):
Use the IDE's built-in file reader / vision capability to view them directly. If the IDE cannot parse the image (returns an error or binary content), note it in the analysis output under "Parsing Limitations" — do not skip the file silently.

**For text files** (.md, .txt, .yaml, .json, etc.):
Use the IDE's built-in file reader as normal.

## Code summary and additional information
* Read `<<OUTPUTS_DIR>>/analysis/specification_index.md` — if it exists, use it to search the specs quickly
* Read `<<OUTPUTS_DIR>>/analysis/repo_summary.md` — if it exists, use it to understand existing code
* If brownfield: scan `<<SRC_DIR>>/` for modules, APIs, and patterns the spec must integrate with or must not re-build
* Read all files from `<<SPEC_DIR>>/` — this is the main resource being evaluated (use `read_document` for non-text formats)

## EARS format definition.
EARS stands for Easy Approach to Requirements Syntax and is a good choice for documenting requirements and acceptance criteria.

EARS format is a single or multiple sentences of the following shape:
* **Generic EARS syntax**: While <optional pre-condition>, when <optional trigger>, the <system name> shall <system response>
* **Ubiquitous requirements**: The <system name> shall <system response>
* **State driven requirements**: While <precondition(s)>, the <system name> shall <system response>
* **Event driven requirements**: When <trigger>, the <system name> shall <system response>
* **Optional feature requirements**: Where <feature is included>, the <system name> shall <system response>
* **Unwanted behaviour requirements**: If <trigger>, then the <system name> shall <system response>
* **Complex requirements**: Combination of the above, e.g. "While <preconditions>, When <triggers>, the <system name> shall <system response>"

# GENERIC ARCHITECTURAL DIMENSIONS FRAMEWORK

**BLOCKING REQUIREMENT: Before deeming a specification complete, it MUST explicitly specify dimensions from ALL applicable parts below.**
**If ANY required dimension is missing or ambiguous, the specification is NOT READY for code generation.**
**Flag ALL missing dimensions as GAP/CRITICAL issues and provide specific options for each.**

This framework prevents different implementations of the same spec by locking all variance-causing decisions.

---
## PART A: UNIVERSAL DIMENSIONS (MANDATORY FOR ALL PROJECTS)

These 6 dimensions apply to EVERY project regardless of type. ALL must be locked.

### A1. DATA PERSISTENCE STRATEGY (Pick exactly ONE primary approach)
☐ **No Persistence**: Stateless, in-memory only, data not saved between runs
☐ **File-Based**: Local filesystem (JSON, YAML, markdown, binary files)
☐ **Embedded Database**: SQLite, LevelDB, or similar (no external server)
☐ **External Database**: PostgreSQL, MySQL, MongoDB, Redis (requires external service)
☐ **Cloud-Managed**: DynamoDB, Firestore, managed services (requires cloud account)
☐ **Hybrid**: Combination (specify which data goes where)

**Must specify**: Primary storage, backup/redundancy needs, data location constraints

### A2. INFRASTRUCTURE COMPLEXITY (Pick exactly ONE level)
☐ **Minimal**: Single process, no containers, local execution only
☐ **Containerized**: Docker/Podman, single-node deployment
☐ **Orchestrated**: Docker Compose, multiple services coordinated
☐ **Cloud-Native**: Kubernetes, service mesh, auto-scaling
☐ **Serverless**: Lambda/Functions, managed infrastructure

**Must specify**: Deployment target, availability requirements, operational complexity budget

### A3. SCALE TARGET (Pick exactly ONE)
☐ **Single User**: Personal tool, no concurrency concerns
☐ **Small Team**: <100 concurrent users/requests
☐ **Department**: 100-1,000 concurrent users/requests
☐ **Organization**: 1,000-10,000 concurrent users/requests
☐ **Public Service**: 10,000+ concurrent users/requests

**Must specify**: Expected peak load, growth projections, performance SLAs

### A4. PRIMARY TECHNOLOGY STACK (All that apply must be specified)
☐ **Primary Language**: Must name exact language and version (Python 3.11, Node 20, Go 1.21, etc.)
☐ **Runtime/Platform**: Must name exact runtime (Docker, Lambda, bare metal, browser, etc.)
☐ **Key Frameworks**: Must name exact frameworks with versions (not "modern framework")
☐ **Package Manager**: Must specify (npm, pip, cargo, go modules, etc.)

**Must specify**: No "flexible" or "team's choice" - lock every technology decision

### A5. QUALITY & TESTING EXPECTATIONS (Pick exactly ONE level)
☐ **Prototype**: No tests required, code review optional
☐ **MVP**: Critical path tests only, basic linting
☐ **Production**: 70%+ coverage, integration tests, CI/CD required
☐ **Enterprise**: 90%+ coverage, E2E tests, security scanning, performance benchmarks

**Must specify**: Test coverage target, CI/CD requirements, code review policy

### A6. SCOPE BOUNDARIES (Must explicitly define)
☐ **In-Scope Features**: Exhaustive list of what WILL be built
☐ **Out-of-Scope Features**: Explicit list of what will NOT be built
☐ **User Types**: Who uses this and what they can do
☐ **External Integrations**: What external services are required vs optional
☐ **Extensibility**: Is this a closed feature set or designed for plugins/extensions?

**Must specify**: Clear boundaries - ambiguous scope = variance

---
## PART B: TECHNOLOGY-SPECIFIC DIMENSIONS (Apply based on project type)

Evaluate which of these categories apply, then lock ALL dimensions in applicable categories.

### B1. USER INTERFACE PROJECTS (Web, Mobile, Desktop, CLI)
If project has a user interface, lock ALL of these:

☐ **B1.1 UI Framework**: Exact framework (React 18, Vue 3, SwiftUI, Flutter, Electron, etc.)
☐ **B1.2 Component Strategy**:
  - Pre-built library (shadcn, MUI, Chakra) → faster, less custom
  - Headless primitives (Radix, Headless UI) → more work, more control
  - Custom from scratch → maximum control, most work
☐ **B1.3 Styling Approach**: CSS modules, Tailwind, styled-components, vanilla CSS, etc.
☐ **B1.4 State Management**: Local state only, Context, Redux, Zustand, MobX, etc.
☐ **B1.5 Routing Strategy**: File-based, config-based, framework-native, etc.

### B2. API/SERVICE PROJECTS (REST, GraphQL, gRPC, WebSocket)
If project exposes or consumes APIs, lock ALL of these:

☐ **B2.1 API Style**: REST, GraphQL, gRPC, WebSocket, or hybrid
☐ **B2.2 Framework**: Express, FastAPI, NestJS, Gin, etc.
☐ **B2.3 Serialization**: JSON, Protocol Buffers, MessagePack, etc.
☐ **B2.4 Versioning Strategy**: URL versioning, header versioning, no versioning
☐ **B2.5 Documentation**: OpenAPI/Swagger, GraphQL introspection, manual docs, none

### B3. AUTHENTICATION & AUTHORIZATION PROJECTS
If project has auth, lock ALL of these:

☐ **B3.1 Auth Method**: Session-based, JWT, OAuth2, API keys, mTLS, or none
☐ **B3.2 Identity Provider**: Self-managed, Auth0, Cognito, Firebase Auth, Keycloak, etc.
☐ **B3.3 Authorization Model**: RBAC, ABAC, ACL, custom, or none
☐ **B3.4 Session Storage**: Database, Redis, in-memory, stateless tokens
☐ **B3.5 Token Handling**: Cookie-based, localStorage, httpOnly, refresh tokens

### B4. DATA PROCESSING PROJECTS (ETL, ML, Analytics)
If project processes data at scale, lock ALL of these:

☐ **B4.1 Processing Model**: Batch, streaming, hybrid
☐ **B4.2 Pipeline Framework**: Airflow, Prefect, Dagster, custom, none
☐ **B4.3 Compute Platform**: Local, Spark, Dask, Ray, cloud functions
☐ **B4.4 Data Format**: Parquet, CSV, JSON, Avro, database tables

### B5. REAL-TIME PROJECTS (Chat, Gaming, Collaboration)
If project has real-time requirements, lock ALL of these:

☐ **B5.1 Transport**: WebSocket, SSE, polling, WebRTC
☐ **B5.2 Message Broker**: None, Redis Pub/Sub, Kafka, RabbitMQ, cloud-native
☐ **B5.3 Presence/State**: How to track online users, sync state
☐ **B5.4 Conflict Resolution**: Last-write-wins, CRDTs, operational transforms

---
## PART C: PROJECT-SPECIFIC DIMENSIONS (Discover additional variance sources)

**CRITICAL**: Beyond Parts A and B, actively search for project-specific decisions that could cause variance.

Ask: "If two teams implemented this spec independently, what might they do differently?"

Common project-specific dimensions to check:

☐ **C1. Code Organization**:
  - Flat structure vs nested modules?
  - Monorepo vs polyrepo?
  - Feature-based vs layer-based organization?
  - Shared code packages vs duplication?

☐ **C2. Error Handling Strategy**:
  - Manual try-catch everywhere?
  - Centralized error middleware?
  - Framework-native error handling?
  - Error reporting service (Sentry, etc.)?

☐ **C3. Logging & Observability**:
  - Console.log vs structured logging?
  - Log aggregation service?
  - Metrics collection?
  - Distributed tracing?

☐ **C4. Configuration Management**:
  - Environment variables only?
  - Config files (JSON, YAML)?
  - Secret manager integration?
  - Feature flags?

☐ **C5. Domain-Specific Patterns**:
  - For e-commerce: payment provider, inventory model, order state machine
  - For healthcare: HIPAA compliance, audit logging, data encryption
  - For finance: transaction handling, reconciliation, regulatory compliance
  - For IoT: device protocol, edge computing, data aggregation
  - For ML: model serving, feature store, experiment tracking

**If you identify ANY decision that could reasonably be made differently by different teams, flag it as a dimension that must be locked.**

---
## PART D: MICRO-LEVEL CONSISTENCY LOCKS (AGGRESSIVE ENFORCEMENT)

**PURPOSE**: Eliminate variance WITHIN implementation phases. Even with locked dimensions, developers can make different micro-decisions. These locks prevent that.

### D1. NAMING CONVENTIONS (Must specify ALL)
☐ **Files**: kebab-case, camelCase, PascalCase, snake_case?
☐ **Directories**: Singular or plural? (user vs users, component vs components)
☐ **Functions**: Verb-first (getUser), noun-first (userGet), or framework convention?
☐ **Variables**: camelCase, snake_case, SCREAMING_SNAKE for constants?
☐ **Components**: PascalCase with suffix (UserCard, UserService)?
☐ **Database**: snake_case tables, singular or plural names?
☐ **API Endpoints**: /users/:id or /user/:id? Plural or singular resources?

### D2. CODE PATTERNS (Must specify ALL that apply)
☐ **Async Style**: async/await, Promises, callbacks, or framework convention?
☐ **Error Throwing**: Throw exceptions, return Result types, return null?
☐ **Null Handling**: null, undefined, Optional type, or never allow null?
☐ **Import Style**: Named imports, default imports, namespace imports?
☐ **Export Style**: Named exports, default exports, barrel files?
☐ **Function Length**: Max lines per function (e.g., 50 lines)?
☐ **File Length**: Max lines per file (e.g., 300 lines)?

### D3. FILE ORGANIZATION (Must specify exact structure)
☐ **Project Root Structure**: Exact top-level directories and their purposes
☐ **Source Code Location**: src/, lib/, app/, or root-level?
☐ **Test Location**: __tests__, *.test.ts adjacent, tests/ directory?
☐ **Config Files**: Where do configs live? Root or config/?
☐ **Generated Files**: Where do generated files go?
☐ **Asset Location**: Where do static assets live?

### D4. COMMIT & WORKFLOW STANDARDS (Must specify)
☐ **Commit Granularity**:
  - Atomic (one logical change per commit, 30-50 commits typical)
  - Feature-level (one feature per commit, 10-20 commits typical)
  - Phase-level (one phase per commit, 5-10 commits typical)
☐ **Commit Message Format**: Conventional commits, free-form, ticket-prefix?
☐ **Branch Strategy**: Feature branches, trunk-based, gitflow?

---
## PART E: FEATURE-LEVEL COMPLETENESS (Apply to each feature)

**For ANY feature mentioned in the spec, check that mandatory sub-tasks are addressed:**
- Infrastructure requirements (databases, storage, CDN, queues)
- Security requirements (authentication, authorization, input validation)
- Error handling requirements
- Testing requirements

**If a feature is mentioned but sub-tasks are not specified, flag as GAP/CRITICAL.**

**Example**: If spec says "Users can upload profile pictures" but doesn't mention compression, storage backend, validation, or CDN → Flag as GAP/CRITICAL with concrete options.

---
## DIMENSION DISCOVERY CHECKLIST

Before declaring specification complete, ask yourself:

1. "If I gave this spec to 3 different senior engineers, would they all build the SAME thing?"
2. "What decisions am I making implicitly that should be explicit?"
3. "What could an AI code generator reasonably interpret differently?"
4. "Are there any 'obvious' choices that actually have alternatives?"

**Any "it depends" or "we'll figure it out" answer = missing dimension = GAP/CRITICAL**

---
## PART F: INTEGRATION & DEPLOYMENT READINESS

**PURPOSE**: Determine whether the specification contains enough deployment and integration
testing detail to enable automated remote deployment and end-to-end testing after code generation.

Evaluate the specification for the presence of ALL of the following:

| Category | What to look for |
|----------|-----------------|
| Deployment methodology | How to deploy: CI/CD workflow files, deploy commands, infrastructure-as-code |
| Acceptance / e2e testing | Test methodology, scenarios, framework to use (Playwright, Cypress, etc.) |
| Infrastructure | Full infrastructure explanation: cluster, registry, namespace, or equivalent |
| CI/CD pipelines | GitHub Actions workflows (e.g., `deploy.yml`, `e2e-tests.yml`) or equivalent CI system |
| Post-deploy verification | Health check endpoint, base URL, or smoke test instructions |
| Secret management | How secrets reach pods: GitHub Secrets only, ESO + Secret Manager, Vault, etc. |
| Namespace isolation | Multi-tenant strategy: dedicated namespaces, NetworkPolicy, resource quotas |
| Teardown / cleanup | How workspace environments are cleaned up after generation completes |
| Frontend API routing | If app uses Next.js or similar SSR: relative API paths (/api/...) via rewrites, or external hostname baked in via NEXT_PUBLIC_API_URL? The latter breaks E2E tests via port-forward. |

**Classification (deterministic — based on presence of information, not judgment):**

- If the spec describes deployment commands/workflows **AND** acceptance test methodology
  **AND** infrastructure targets → assign label `INTEGRATION_TESTS_READY`
- Otherwise → assign label `LOCAL_ONLY`

**USER NOTICE requirements**: When classifying, identify any operations that require manual user
action (cloud resource provisioning, DNS setup, GitHub secrets, Terraform runs, IAM setup) and list
them explicitly so the operator knows what must be done before deploy.

**Output**: Add a Part F section to the completeness document with this structure:

```markdown
## Part F: Integration & Deployment Readiness

**Integration Readiness:** <INTEGRATION_TESTS_READY or LOCAL_ONLY>

**Rationale:** <2-3 sentences explaining what was found or what is missing>

**Integration Details — Locked Values:**
(only if INTEGRATION_TESTS_READY)

| Dimension | Value |
|-----------|-------|
| Deploy method | <e.g., GitHub Actions: .github/workflows/deploy-dev.yml> |
| Target environment | <e.g., GKE cluster gke-dev-1, namespace myapp-dev> |
| Image registry | <e.g., ghcr.io/myorg/myapp> |
| Base URL | <e.g., https://myapp-dev.internal> |
| Health check | <e.g., /api/health> |
| E2e framework | <e.g., Playwright> |
| E2e test location | <e.g., e2e/> |
| Frontend API routing | <relative paths via rewrites OR external hostname baked in (flag as CONTRADICTION if E2E also required)> |
| Max QA rounds | <default: 3> |
| Secret management | <e.g., ESO + GCP Secret Manager, GitHub Secrets only> |
| Namespace pattern | <e.g., session_id-ws_id> |
| Teardown method | <e.g., kubectl delete namespace + gcloud secrets delete> |
```

**Include Part F in the DIMENSION STATUS summary**:
```
- Part F (Integration Readiness): <INTEGRATION_TESTS_READY or LOCAL_ONLY>
```

# Tasks

Conduct a comprehensive analysis of the provided specification documents to systematically identify, categorize, and evaluate all instances of contradictions, ambiguities, and gaps.

Examine the specification across multiple dimensions including:
- Functional requirements (features users can do)
- Acceptance criteria (how to verify it works)
- Technical requirements (specific technology choices)
- Architectural decisions (Parts A-E dimensions above)
- Non-functional requirements (performance, security, scale)
- Data models (what data is stored and how)
- Error handling (what happens when things fail)
- Dependencies and integrations (external services)
- Feature-level completeness (every feature should have mandatory sub-tasks)

Cross-reference all sections to detect conflicting statements, incompatible requirements, undefined terms, missing information, and logical inconsistencies.

### Issue Type Definitions

You must categorize each issue using EXACTLY ONE of these types:

* **CONTRADICTION**: Two or more statements that directly conflict. Example: "Must support offline mode" vs "Requires real-time server connection", or a Next.js frontend that bakes external NEXT_PUBLIC_API_URL while also requiring E2E via port-forward (browser JS can't reach the external hostname from CI).

* **AMBIGUITY**: Statements that are unclear, vague, or open to multiple interpretations — including architectural decisions not explicitly locked. Examples: "Use appropriate storage", "scalable architecture", undefined technical terms, vague acceptance criteria.

* **GAP**: Missing information, undefined requirements, incomplete specifications. Includes missing answers to ANY required dimensions from Parts A-E above. Examples: missing error-handling spec, undefined storage, missing scale target, missing tech stack, missing acceptance criteria in EARS format, unlocked micro-level conventions.

**CRITICAL**: For common features (file uploads, authentication, CRUD, search, payments), check that all mandatory sub-tasks are present. If a spec says "users can upload files" but doesn't mention compression, storage backend, validation, or moderation, flag these as GAP/CRITICAL.

### Severity Level Definitions

* **CRITICAL**: Issue makes implementation impossible OR causes different teams to implement the same spec 3 different ways. **ALL missing/ambiguous architectural dimension answers must be CRITICAL.**

* **HIGH**: Significantly impacts development effort or quality. Ambiguous API contracts, missing performance requirements for critical paths, unclear auth/authorization model.

* **MEDIUM**: Impacts efficiency or code quality but has workarounds. Unclear validation for non-critical fields, ambiguous UI text, missing edge cases.

* **LOW**: Minor clarification or cosmetic. Typos, formatting, vague descriptions for optional features.

For each identified issue:
1. **Categorize** using EXACTLY ONE type (CONTRADICTION, AMBIGUITY, or GAP)
2. **Assign severity** using EXACTLY ONE level (CRITICAL, HIGH, MEDIUM, or LOW)
3. **Write issueSummary** (max 5 sentences total):
   - 1-2 sentences: What and where in the spec
   - 1 sentence: Concrete impact on implementation
   - 1-2 sentences: Specific resolution
   - **For CRITICAL issues**: provide max 2 concrete options inline: `Option A: [choice] / Option B: [choice]`
   - For GAP issues about missing acceptance criteria: provide exactly 1 EARS-format requirement as the resolution

### Output format
Store the output as a markdown file at **EXACTLY** `<<OUTPUTS_DIR>>/analysis/specification_completeness.md`. The backend contract validator expects this exact filename — any other name (e.g. `analysis.md`, `spec_check.md`) will cause `run_generation` to be rejected with `ANALYSIS_MISSING`.

If the file would exceed ~300 lines, write it in parts (`_part1.md`, `_part2.md`, …) and merge with `cat … > specification_completeness.md && rm …_part*.md`.

The file should have this structure:

- **Dimension Status — Summary** (counts only — see full inventory below)
- **Dimension Status — Full inventory** (mandatory — list **every** dimension; do not stop at "2/6 locked")
- **Critical Issues** subsection (if any exist)
- **High Severity Issues** subsection (if any exist)
- **Medium Severity Issues** subsection (if any exist)
- **Low Severity Issues** subsection (if any exist)
- Each issue should have:
    - **[TYPE] [SEVERITY]: [Issue Title]**
    - Issue summary
    - Recommended resolution (with options for CRITICAL issues)

### Dimension Status — required in the markdown file

The report must include **both** a short summary **and** a complete per-dimension inventory. Summary-only lines like `Part A: 2/6 locked` are insufficient without listing all six rows.

**Summary block** (at top of the Dimension Status section):

```markdown
## Dimension Status — Summary

- Part A (Universal): X/6 locked
- Part B (Tech-Specific): X/Y applicable, Z locked
- Part C (Project-Specific): X discovered, Y locked
- Part D (Micro-Level): X/Y conventions locked
- Part E (Feature Completeness): X features fully specified
- Part F (Integration Readiness): INTEGRATION_TESTS_READY | LOCAL_ONLY
```

**Full inventory** (immediately after the summary — one table or bullet list per part):

**Part A — list all 6** (A1–A6 from the framework above). For each row: dimension id, name, status (`LOCKED` | `GAP` | `N/A`), and where it is specified in the spec (file/section) or what is missing.

**Part B — list every applicable category and sub-dimension** (B1.1–B1.5, B2.1–B2.5, … only for categories that apply; mark non-applicable categories `N/A` with one-line rationale).

**Part C — list every discovered project-specific dimension** from the checklist (C1–C5 plus any extras you identified). If none beyond Parts A/B, state that explicitly and still show C1–C5 with status.

**Part D — list all convention groups** (D1–D4 and each bullet under them that applies). Mark non-applicable items `N/A`.

**Part E — list each named feature** from the spec with `FULLY_SPECIFIED` | `INCOMPLETE` and which sub-tasks are missing.

**Part F — integration readiness** (repeat the Part F section from the framework: label, rationale, and the Integration Details table when `INTEGRATION_TESTS_READY`).

Example row format:

```markdown
| ID | Dimension | Status | Evidence / gap |
| A1 | Data persistence strategy | GAP | No primary storage named in specs/... |
| A2 | Infrastructure complexity | LOCKED | docs/kubernetes/evergreen.md — GKE Autopilot |
...
```

At the end of your final message:
- Mention where the analysis result is stored
- Repeat only the **summary** block (counts + Part F label) — the file must already contain the **full inventory**
- If ALL applicable dimensions are locked AND no CRITICAL issues exist:
  "SPECIFICATION READY — I have all the information I need. Next step: call `run_planning` to produce `<<OUTPUTS_DIR>>/planning/IMPLEMENTATION_PLAN.md`."
- Otherwise:
  "SPECIFICATION NOT READY — MUST RESOLVE BEFORE CODE GENERATION:
  - Missing Part A dimensions: [list]
  - Missing Part B dimensions: [list if applicable]
  - Missing Part C dimensions: [list if discovered]
  - Missing Part D conventions: [list]
  - Incomplete features: [list]
  - Critical issues: [list]"
