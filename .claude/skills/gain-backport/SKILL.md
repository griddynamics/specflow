---
name: gain-backport
description: Classify a fix made to a GAIN-generated workspace app and propagate it to the right level — GAIN harness prompt, spec template, or documentation. Prevents the same issue from recurring in future runs.
argument-hint: "<describe the fix you made or want to make>"
---

# GAIN Backport

You are helping a user classify a fix discovered in a GAIN-generated workspace app and propagate it to the correct level so future runs don't have the same problem.

## Input

The user provided: `$ARGUMENTS`

If empty, ask: "Describe the fix — what was broken, what you changed, and which file(s) you edited in the workspace repo."

---

## Step 1 — Gather context

Ask these questions (all at once, not one by one):

1. **What was the symptom?** (error message, test failure, deployment failure, app misbehaviour)
2. **What file(s) did you change?** (e.g. `helm/recipe-ai/templates/backend-deployment.yaml`, `backend/src/config.py`)
3. **Is this fix specific to this app/spec, or would any GAIN-generated app need it?**
4. **Did the deploy agent fix this on its own in a previous iteration, or did you have to fix it manually?**
5. **Are you currently in the GAIN harness repo, the workspace repo, or neither?** (Run `pwd` if unsure)

---

## Step 2 — Classify using the decision tree

Use the answers to classify the fix:

```
Does every GAIN-generated app need this fix regardless of what they do?
  YES → Level 1 or 2

  Is the fix about what the agent generates (prompts, spec completeness checks)?
    YES → Level 1 (GAIN harness — agents_claude_code.py)
    NO  → Level 2 (spec template — AGENT_GUIDE.md or deployment spec)

Does it only affect certain types of apps (e.g. those using postgres, those with LLM integration)?
  YES → Level 2 (spec template — conditional pattern)

Did the deploy agent fix this on its own without human intervention?
  YES → Level 3 (acceptable deployment friction — document it, no code change needed)

Is it specific to this one run / deployment environment?
  YES → Level 4 (run-specific — no propagation)
```

Tell the user the classification and explain why before proceeding.

---

## Step 3 — Take action based on level

### Level 1 — GAIN harness fix

**Target file**: `backend/app/prompts/agents_claude_code.py` in the GAIN harness repo.

If the user is in the GAIN harness repo:
- Read the relevant section of `agents_claude_code.py`
- Identify the right place to add the fix: Part F spec completeness table, `_deployment_instructions()`, or the CONTRADICTION definition
- Propose the change with a clear before/after diff
- Ask: "Apply this change? (yes/no)"
- If yes: apply the edit, run `make unit-tests`, confirm tests pass
- Produce a commit message and ask if the user wants to commit

If the user is NOT in the GAIN harness repo:
- Describe exactly what needs to change and in which function/section
- Produce a ready-to-apply patch the user can apply when they're in the right repo

---

### Level 2 — Spec template fix

**Target file**: `specs/deployment/AGENT_GUIDE.md` in the workspace repo (this file is copied to each new workspace at generation time).

Sub-cases:

**A — Fix the spec in the current workspace** (affects this workspace + future workspaces via the GAIN harness template):

If the user is in the workspace repo:
- Read the relevant section of `specs/deployment/AGENT_GUIDE.md`
- Propose the change
- Ask: "Apply this change? (yes/no)"
- If yes: apply, then ask if they want a PR:
  ```bash
  git checkout -b fix/spec-{short-description}
  git add specs/deployment/AGENT_GUIDE.md
  git commit -m "spec: {description of fix}"
  git push -u origin fix/spec-{short-description}
  gh pr create --title "spec: {description}" \
    --body "Backport fix from GAIN run. See gain-user-experience-guide.md for context."
  ```
  After merge, remind them to trigger a redeploy.

**B — Also update the GAIN harness** so future generated workspaces get this spec from day one:
- Tell the user: "This spec change should also be applied to the GAIN harness so future generated workspaces have it. The source template is in the GAIN harness repo — propagate it there when you have a chance."

---

### Level 3 — Document it (no code change needed)

The deploy agent fixed this autonomously. No propagation required, but it's worth noting if it increased the deploy iteration count.

Output:
```
Classification: Level 3 — Run-level detail (agent fixed autonomously)

This is expected deployment friction. The agent is designed to absorb these.
No code changes needed in the harness or spec.

If this fix took more than 2–3 deploy iterations to reach:
→ Consider whether a Level 2 spec change would give the agent a better starting point.

Document in your run notes if keeping a record of iteration sources.
```

---

### Level 4 — Run-specific, no action needed

```
Classification: Level 4 — Run-specific

This fix is specific to this deployment/environment and won't recur in other runs.
No propagation needed.
```

---

## Step 4 — Final summary

Output a summary of what was classified and what was done (or what the user should do):

```
Backport summary
────────────────
Fix: [brief description]
Classification: Level [N] — [label]
Action taken: [what was done or what needs to be done]
Propagates to: [future runs? this workspace only? nowhere?]
```
