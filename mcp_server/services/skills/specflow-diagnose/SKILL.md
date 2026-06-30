---
name: specflow-diagnose
description: Diagnose errors and symptoms from a SpecFlow-deployed app. Collects observables (logs, error messages, infra state), identifies root cause through guided discovery, and proposes fixes appropriate to each actor — SpecFlow agent re-run, operator intervention, or spec backport.
argument-hint: "<error message, symptom, or 'help' to start guided collection>"
---

# SpecFlow Diagnose

You are diagnosing a problem with a SpecFlow-generated app. Your job is to collect the right observables, locate the root cause in the right layer of the system, and propose a fix that respects who is responsible for each layer.

## Input

The user provided: `$ARGUMENTS`

If `$ARGUMENTS` is empty or "help", start with [Step 1 — Collect observables](#step-1--collect-observables).

Otherwise treat `$ARGUMENTS` as the initial symptom description and proceed directly to Step 1, using it as your starting point.

---

## Background: layers and actors

A SpecFlow-deployed app has three layers. Each layer has a different responsible actor.

| Layer | What it covers | Who fixes it |
|---|---|---|
| **App code** | Backend logic, frontend, API contracts, business rules, DB migrations | SpecFlow agent (re-run deploy workflow) or developer editing workspace repo |
| **Deployment config** | Helm chart values, GHA workflow steps, secret references, probe settings, resource limits | SpecFlow agent (re-run deploy) or operator patching the workspace repo |
| **Infra state** | Running pods, secrets in-cluster, PVC data, namespace, cluster-level config | Human operator via `kubectl` — SpecFlow agent cannot touch this directly |

Fixes that require changing files in the workspace repo can be committed and re-triggered via the deploy workflow. Fixes that require patching live cluster state must be done by a human operator.

---

## Step 1 — Collect observables

Ask the user to share whatever they have. Do not assume — ask explicitly for each category that might be relevant given the initial symptom. Group your questions:

**A — Error signals**
- Exact error message or log line (copy-paste, not paraphrase)
- Which job/step in GitHub Actions surfaced it, if applicable
- What the user saw in the browser or CLI

**B — System state**
- Output of `kubectl get pods -n <namespace>` (ask for namespace if unknown; format is `specflow-{generation_id}-{workspace_id}`)
- Any pod logs they have (`kubectl logs <pod> -n <namespace> --tail=50`)
- GHA run URL or job name that failed

**C — Context**
- Is this a fresh deploy or a redeploy of an existing namespace?
- Did this work before? If so, what changed?
- Which workspace repo and generation ID?

Collect what the user can provide. Do not block on missing items — proceed with what you have and note what's unknown.

---

## Step 2 — Locate the signal in the system

Classify each observable into its layer:

**App code signals**
- Errors in backend logs from application logic (unhandled exceptions, import errors, business rule failures)
- Test failures (unit, integration, E2E) that point to incorrect application behaviour
- Frontend console errors related to API response shapes or missing data

**Deployment config signals**
- GHA job failures in `build-push`, `bootstrap`, `deploy`, or `e2e` steps
- Helm template errors or `kubectl apply` failures
- `alembic` / migration errors during deploy
- Pod startup failures caused by missing env vars or misconfigured probes
- Secret reference errors (secret exists but wrong key name, or not mounted)

**Infra state signals**
- Pods in `CrashLoopBackOff`, `Pending`, or `OOMKilled` after a previously successful deploy
- PVC or storage issues (data directory problems, mount failures)
- Namespace missing entirely
- Auth/WIF errors from cluster-side identity configuration
- Network policy or service mesh issues

A single failure often has signals in multiple layers. Identify each layer present before jumping to a fix.

---

## Step 3 — Discover the root cause

For each signal, drive discovery to the actual root cause — not the surface error. Ask the user to run commands or share files as needed.

**For app code signals**: read the relevant source files in the workspace repo. Trace the error back to the code path. Look at the test that failed, the function that threw, the migration that broke.

**For deployment config signals**: read the relevant GHA workflow steps and Helm templates. Check what env vars are expected vs what's actually set. Read the deploy job logs line by line around the failure.

**For infra state signals**: gather more kubectl output. Check pod events (`kubectl describe pod`), check what's actually in secrets, check PVC contents if relevant. The goal is to distinguish between "the config tells the pod to do X but X is wrong" (config problem) vs "the pod is in a bad state independent of config" (infra state problem).

Keep asking until you can state the root cause in one sentence: **what is wrong, in which file or resource, and why it causes the observed failure**.

---

## Step 4 — Propose fixes by actor

Once root cause is identified, propose fixes grouped by who must act and in what order.

### Fix type A — Re-run the deploy workflow (SpecFlow agent)

Use when: the fix is a change to files in the workspace repo (app code, Helm values, GHA workflow, specs). The SpecFlow deploy agent can re-run and apply the fix autonomously.

Propose:
1. The exact file(s) to edit and what to change
2. The commit and push commands
3. How to trigger a redeploy:
   ```bash
   gh workflow run deploy.yml \
     --repo <workspace-repo> \
     --ref main
   ```
4. What to watch for in the new run to confirm the fix worked

### Fix type B — Operator intervention (kubectl)

Use when: the fix requires patching live cluster state — secrets, env vars on running deployments, statefulset config, or namespace-level resources. This cannot be done by the SpecFlow agent; a human must run kubectl.

Propose:
1. The exact kubectl commands to run
2. The expected output that confirms success
3. Whether a pod restart is needed after patching
4. Whether the fix should also be persisted in the workspace repo (to survive a future redeploy)

### Fix type C — Backport to SpecFlow (spec or prompt)

Use when: the root cause is something the SpecFlow agent generated incorrectly and will generate incorrectly again in future runs. The fix should be propagated so it doesn't recur.

Note the classification and tell the user: "This fix should be backported so future SpecFlow runs don't hit the same issue. Use `/specflow-backport` to classify and propagate it."

---

## Step 5 — Output

After proposing fixes, output a structured summary:

```
## Diagnosis

Root cause: [one sentence]
Layer: [App code / Deployment config / Infra state]

Fixes:
  [A] Re-run deploy after: [what to change in repo]
  [B] Operator action: [kubectl commands]
  [C] Backport: [yes/no — what to propagate]

Confidence: [High / Medium / Low — and why if not High]

If the fix doesn't resolve it: [what to check next]
```

If confidence is low (root cause still ambiguous), say so explicitly and list what additional observables would narrow it down further.
