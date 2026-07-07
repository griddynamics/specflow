---
name: creating-commits
description: Create well-structured git commits with clear, conventional commit messages. Use this skill whenever the user asks to commit changes, "commit this", "save my work", "make a commit", "write a commit message", or after completing a coding task when changes need to be committed. Also use when the user asks to split changes into multiple commits, amend a commit, or improve an existing commit message.
---

# Creating Commits

A skill for producing clean, atomic commits with high-quality messages.

## Workflow

Always follow these steps in order:

### 1. Inspect before committing

Run these in parallel to understand the state of the repo:

```bash
git status                 # what's staged, unstaged, untracked
git diff                   # unstaged changes
git diff --staged          # staged changes
git log --oneline -10      # recent messages, to match repo style
```

Never commit blind. Read the actual diff — the commit message must describe what changed, not what you think you changed.

### 2. Decide what belongs in the commit

- **One logical change per commit.** If the diff contains unrelated changes (e.g., a bug fix AND a refactor AND a config tweak), propose splitting into separate commits. Use `git add -p` or stage files selectively.
- **Never use `git add .` or `git add -A` reflexively.** Stage files explicitly by path so unrelated or accidental files (logs, `.env`, build artifacts, editor files) don't slip in.
- Check for files that should not be committed: secrets, credentials, large binaries, generated output. Warn the user if any appear in the diff.
- Do not commit commented-out code or leftover debug statements without flagging them first.

### 3. Write the message

Detect the repo's convention from `git log`. If the repo uses Conventional Commits, follow it. Otherwise use the standard format below.

**Format:**

```
<type>(<optional scope>): <summary, imperative, ≤50 chars>

<body: what and WHY, wrapped at 72 chars — optional for trivial changes>
```

**Types** (Conventional Commits): `feat`, `fix`, `refactor`, `perf`, `docs`, `test`, `build`, `ci`, `chore`, `style`, `revert`.

**Subject line rules:**
- Imperative mood: "add validation", not "added" or "adds". Test: the line should complete "If applied, this commit will ___".
- ≤50 characters, no trailing period, lowercase after the type prefix.
- Be specific: `fix: prevent race condition in session refresh` — not `fix: bug fix` or `update code`.

**Body rules (when needed):**
- Explain *why* the change was made and any non-obvious consequences; the diff already shows *what*.
- Mention alternatives considered or constraints if relevant.
- Skip the body only for genuinely trivial changes (typo fixes, version bumps).

**Never:**
- Vague messages: "fixes", "wip", "updates", "misc changes".
- Restating the diff line-by-line.
- Bundling "and also..." — that's a sign the commit should be split.
- Adding footer and information about co-author like Claude etc.

### 4. Commit

Use a heredoc for multi-line messages:

```bash
git commit -m "$(cat <<'EOF'
fix(auth): prevent race condition in token refresh

Two concurrent requests could both detect an expired token and
trigger duplicate refresh calls, invalidating each other's sessions.
Serialize refresh behind a per-user mutex.

Fixes #482
EOF
)"
```

### 5. Verify

Run `git status` and `git log -1 --stat` after committing to confirm the commit contains exactly what was intended.

## Special cases

- **Amending**: only `git commit --amend` when the commit has NOT been pushed, and confirm with the user first.
- **Empty diff**: if there is nothing to commit, say so — do not create empty commits.
- **Pre-commit hooks**: if a hook fails, fix the underlying issue rather than using `--no-verify`. Only bypass hooks if the user explicitly asks. If a hook modifies files (e.g., a formatter), re-stage and retry once.
- **Never push, force-push, or rewrite history** unless the user explicitly asks.
- **Breaking changes**: mark with `!` after the type (`feat!:`) and a `BREAKING CHANGE:` footer explaining migration.

## Examples

Good:

```
feat(api): add cursor-based pagination to /orders endpoint

Offset pagination degraded past ~100k rows. Cursor pagination keeps
response times flat and matches the pattern used in /customers.
```

```
chore: bump lodash to 4.17.21
```

Bad:

```
update stuff
fixed the thing we talked about
WIP
feat: changes to api and also fixed tests and updated readme
```
