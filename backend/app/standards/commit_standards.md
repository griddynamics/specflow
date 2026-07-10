# Commit Standards

## Purpose
This document defines commit hygiene standards for code generation workflows to ensure:
- Accurate P10Y metrics tracking
- Clear component attribution
- Proper granularity for generation breakdowns
- Consistent commit history

CRITICAL: the main reason to standardize commits is REPEATABILITY. We perform commits using guidelines, and therefore a similar piece of work or change will have similar commit size/complexity.
CRITICAL: add to .gitignore folders like .venv, node_modules, package-lock.json - relevant to the tech stack

## Granularity Guidelines

### Ideal Commit Size
- **50-300 lines of code changed** (optimal range)
- Single logical unit of work
- One component or closely related components
- Atomic: can be reverted without breaking unrelated features
- Complete with tests (when applicable)

### When to Commit ✅

**DO commit when:**
- After implementing a complete feature component
- After adding a new API endpoint with tests
- After creating a new database model with migrations
- After implementing a UI component with styles and logic
- After completing a logical refactoring unit
- After setting up infrastructure or configuration that works end-to-end
- After writing a batch of related tests for a feature

**Examples of good commits:**
- `backend_implement JWT token generation and validation`
- `frontend_add user profile form with validation`
- `database_create users and roles tables with migrations`
- `api_add REST endpoints for product catalog`
- `infrastructure_setup Docker compose with PostgreSQL and Redis`
- `testing_add integration tests for authentication flow`

### When NOT to Commit ❌

**DO NOT commit when:**
- After changing a single line or fixing a typo (bundle these)
- After completing the entire application (too large, split it up)
- In the middle of implementing a feature
- When code doesn't compile or has obvious errors

**Examples of bad commits:**
- `Update everything` (too broad, no clear component)
- `Fix typo` (too small, should be bundled with feature work)
- `WIP` (incomplete work, not atomic)
- `Add all files` (too large, no clear scope)
- `Changes` (meaningless message)

## Commit Message Format

### Standard Format (first line of commit message)

Use an **underscore** after the component token (metadata is parsed from `git log`, not from a JSON file):

```
<component>_<action> <subject>
```

Example: `backend_implement JWT token generation`

Commits whose subject starts with **`SKIP_`** (case-insensitive) are **excluded** from P10Y / generation (e.g. `SKIP_initial_user_source` for user-provided seed code).

### Components
Stick to already known component names, choose the closest matching name.
Valid component identifiers:
- `frontend` - UI/client-side code
- `backend` - Server-side application logic
- `api` - API endpoints and contracts
- `database` - Database schemas, migrations, models
- `auth` - Authentication and authorization
- `infrastructure` - Docker, deployment, CI/CD
- `testing` - Test code, test infrastructure
- `documentation` - README, docs, comments
- `pipeline` - data pipelines, orchestration of data projects
- `ml` - machine learning and data science, features, model training, A/B tests, evaluation, notebooks
- `mobile` - Mobile clients, native apps, cross-platform app manifests and build configuration
- `common` - Cross-cutting concerns, project setup

### Actions
Common action verbs:
- `implement` - New feature or functionality
- `add` - New files, dependencies, or resources
- `update` - Modify existing functionality
- `fix` - Bug fixes
- `refactor` - Code restructuring without functionality change
- `remove` - Delete code or files
- `configure` - Configuration changes
- `optimize` - Performance improvements

### Examples

**Good commit messages:**
```
backend_implement JWT token generation
frontend_add user profile form component
database_create initial schema with users table
api_add REST endpoints for order management
infrastructure_configure Docker Compose for local development
testing_add unit tests for payment service
common_setup project structure and dependencies
mobile_configure app manifest and build settings
frontend_refactor state management to use Redux Toolkit
backend_fix validation error handling in user endpoints
```

**Bad commit messages:**
```
update stuff
fix
changes
wip
backend do things (missing underscore after component)
implement everything (too broad)
```

## Component Attribution Rules

### Single Component Changes
When changes affect only one component:
- Single commit with single component identifier
- Example: `backend_implement password hashing`

### Multi-Component Changes
When a feature requires changes across multiple components:
- **Sequential commits per component** (preferred)
- Each commit focuses on one component's changes
- Maintains clear attribution for generation

**Example sequence for a user registration feature:**
1. `database_create users table and migration`
2. `backend_implement user registration service`
3. `api_add user registration endpoint`
4. `frontend_add registration form component`
5. `testing_add integration tests for user registration`

### Cross-Cutting Changes
For changes that truly affect multiple components simultaneously:
- Prefer several single-component commits; if one commit must cover everything, use `common_<description>`

## Metadata for P10Y (no JSON sidecar)

Generation reads **`git log`** (oldest first, no merges). Each included commit’s subject is split on the **first** underscore for component grouping.

## Commit Workflow

### Step-by-Step Process

1. **Complete a logical unit of work**
   - Feature is implemented
   - Tests are written and passing
   - Code is linted and formatted
   - No obvious errors or TODOs

2. **Stage relevant files**
   ```bash
   git add <files related to this component>
   ```

3. **Create commit with proper message**
   ```bash
   git commit -m "component_action and subject"
   ```

4. **Push commit**
   ```bash
   git push origin main
   ```

5. **Optional check**
   ```bash
   git log -1 --oneline
   ```

## Anti-Patterns to Avoid

### The "Big Bang" Commit
❌ **Problem**: One massive commit with entire application
- Impossible to attribute to specific components
- Can't track granular progress
- Difficult to review or debug

✅ **Solution**: Break work into 10-30 commits representing logical progression

### The "Micro" Commits
❌ **Problem**: 100+ commits each changing 1-2 lines
- Overhead in commit management
- Difficult to map to meaningful work units
- Inflates metrics without adding value

✅ **Solution**: Bundle related small changes into logical commits

### The "Mixed Bag" Commit
❌ **Problem**: One commit touching frontend, backend, database, tests, docs
- Can't attribute to specific component
- Breaks atomicity principle
- Difficult to revert if needed

✅ **Solution**: Split into sequential commits per component

### The "Vague Message" Commit
❌ **Problem**: Messages like "update", "fix", "changes"
- No context for what was changed
- Can't correlate with requirements
- Poor documentation for future reference

✅ **Solution**: Use descriptive format: `component_action and subject`

