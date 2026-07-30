# Commit Standards

## Purpose
This document defines commit hygiene standards for code generation workflows to ensure:
- Accurate P10Y metrics tracking
- Clear phase attribution (which implementation-plan phase produced the work)
- Proper granularity for generation breakdowns
- Consistent commit history

CRITICAL: the main reason to standardize commits is REPEATABILITY. We perform commits using guidelines, and therefore a similar piece of work or change will have similar commit size/complexity.
CRITICAL: add to .gitignore folders like .venv, node_modules, package-lock.json - relevant to the tech stack

## Granularity Guidelines

### Ideal Commit Size
- **50-300 lines of code changed** (optimal range)
- Single logical unit of work
- One feature or closely related change
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

**Examples of good commits** (you write the plain subject; the hook adds the phase prefix):
- `implement JWT token generation and validation`
- `add user profile form with validation`
- `create users and roles tables with migrations`
- `add REST endpoints for product catalog`
- `setup Docker compose with PostgreSQL and Redis`
- `add integration tests for authentication flow`

### When NOT to Commit ❌

**DO NOT commit when:**
- After changing a single line or fixing a typo (bundle these)
- After completing the entire application (too large, split it up)
- In the middle of implementing a feature
- When code doesn't compile or has obvious errors

**Examples of bad commits:**
- `Update everything` (too broad)
- `Fix typo` (too small, should be bundled with feature work)
- `WIP` (incomplete work, not atomic)
- `Add all files` (too large, no clear scope)
- `Changes` (meaningless message)

## Commit Message Format

### Standard Format (first line of commit message)

Write a plain, descriptive first line:

```
<action> <subject>
```

Example: `implement JWT token generation`

**Do NOT add a component or phase prefix yourself.** During generation a `prepare-commit-msg` git
hook automatically prepends the current implementation-plan phase as `p<NN>_` (for example
`p07_implement JWT token generation`). P10Y groups commits by that phase prefix, so attribution is
deterministic and does not depend on your commit hygiene. Metadata is parsed from `git log`, not
from a JSON file.

Commits whose subject starts with **`SKIP_`** (case-insensitive) are **excluded** from P10Y /
generation (e.g. `SKIP_initial_user_source` for user-provided seed code). The hook never adds a
phase prefix to `SKIP_` commits.

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
implement JWT token generation
add user profile form component
create initial schema with users table
add REST endpoints for order management
configure Docker Compose for local development
add unit tests for payment service
setup project structure and dependencies
```

**Bad commit messages:**
```
update stuff
fix
changes
wip
implement everything (too broad)
```

## Phase Attribution (automatic)

Generation runs phase-by-phase from the implementation plan. Before each phase the harness records
the active phase number, and the `prepare-commit-msg` hook stamps every commit you make during that
phase with `p<NN>_`. Each included commit's subject is split on the **first** underscore for phase
grouping.

- One commit belongs to exactly one phase — the phase active when you committed.
- Prefer several small commits within a phase over one big commit.
- Commits made outside a codegen phase (initial seed, deployment) are left unphased and reported
  separately; you never need to manage the prefix yourself.

## Commit Workflow

### Step-by-Step Process

1. **Complete a logical unit of work**
   - Feature is implemented
   - Tests are written and passing
   - Code is linted and formatted
   - No obvious errors or TODOs

2. **Stage relevant files**
   ```bash
   git add <files related to this change>
   ```

3. **Create commit with a plain descriptive message** (the hook adds the phase prefix)
   ```bash
   git commit -m "implement user registration service"
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
- Impossible to attribute to specific phases
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
❌ **Problem**: One commit touching many unrelated areas at once
- Breaks atomicity principle
- Difficult to revert if needed

✅ **Solution**: Split into sequential, focused commits

### The "Vague Message" Commit
❌ **Problem**: Messages like "update", "fix", "changes"
- No context for what was changed
- Can't correlate with requirements
- Poor documentation for future reference

✅ **Solution**: Use a descriptive `<action> <subject>` first line
