# Technology Stack Dimensions

## Purpose

This document defines **what technology decisions must be explicitly locked** before code generation
can produce consistent results. Two independent teams given the same spec must arrive at the same
stack — every ambiguous choice is a variance source.

CRITICAL: if the specification does not specify a decision, flag it as a GAP. "Modern framework" or
"team's choice" are not acceptable answers — every dimension below must have a concrete, named value.

For detailed guidance on specific technologies, architecture patterns, or stack trade-offs, use the
**Rosetta KnowledgeBase MCP** (`get_context_instructions` / `query_instructions` / `list_instructions`) to retrieve up-to-date recommendations.

---

## Required Dimensions

Every project must lock ALL of the following that apply. Each blank is a potential variance point.

### Language & Runtime
- Primary language + exact version (e.g., Python 3.12, Node 20 LTS, Go 1.22)
- Runtime environment (container, serverless function, bare metal, browser)
- Package manager (pip/uv, npm/pnpm, go modules, cargo)

### Application Framework
- Web / API framework + version (e.g., FastAPI 0.110, Express 4, NestJS 10)
- If frontend: UI framework + version (React 18, Vue 3, Angular 17, etc.)
- If SSR: framework variant (Next.js App Router, Nuxt 3, SvelteKit, etc.)

### Data Layer
- Primary database type and product (PostgreSQL 16, MongoDB 7, SQLite, DynamoDB, etc.)
- ORM / query builder (SQLAlchemy 2, Prisma, Drizzle, none / raw queries)
- Migration strategy (Alembic, Flyway, Prisma migrate, manual)
- Secondary stores if any: cache (Redis), search (Elasticsearch), queue (RabbitMQ, Kafka)

### Authentication & Authorization
- Auth mechanism (JWT, session cookies, OAuth2/OIDC, API keys, mTLS, none)
- Identity provider (self-managed, Auth0, Cognito, Firebase, Keycloak, etc.)
- Authorization model (RBAC, ABAC, ACL, none)

### Frontend (if applicable)
- Component strategy: pre-built library (shadcn, MUI), headless primitives (Radix), or custom
- Styling approach (Tailwind, CSS Modules, styled-components, vanilla CSS)
- State management (Redux Toolkit, Zustand, Context API, server state only via React Query)
- Routing (React Router, Next.js App Router, TanStack Router, etc.)

### Testing
- Unit test framework (pytest, Vitest, Jest, Go testing)
- Integration / API test approach (httpx, Supertest, etc.)
- E2E framework if required (Playwright, Cypress — or explicitly "none")
- Coverage target (or explicitly "not enforced")
- For JVM hanging-command triage (any JVM language), see `./standards/jvm_hanging_tests.md`
- For Kotlin coroutines test hang patterns, see `./standards/kotlin_coroutines_tests.md`

### Build & Tooling
- Build tool (Vite, webpack, esbuild, Poetry, uv, Gradle, etc.)
- Linter + formatter (ESLint + Prettier, ruff + black, golangci-lint, etc.)
- Container base image strategy (slim, alpine, distroless, custom)

### Deployment Artifacts
- Container orchestration target (K8s, ECS, Cloud Run, docker-compose only, etc.)
- CI/CD platform (GitHub Actions, GitLab CI, Jenkins, etc.)
- Infrastructure-as-Code tool if any (Terraform, Pulumi, CDK, none)

---

## Discovering Missing Dimensions

When reviewing a specification, ask: "If two senior engineers implemented this independently,
what technology choices might they make differently?" Each answer to that question is a dimension
that must be locked.
