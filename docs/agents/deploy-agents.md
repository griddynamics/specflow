# Deploy & QA Agents — Behavior Specification

## Overview

Deploy and QA agents run after code generation to trigger deployment pipelines via GitHub Actions and verify the deployed application. This document captures the required behavior, access contracts, and failure protocols learned from initial runs.

---

## Context Provided to Agent at Launch

The agent **must not discover** `workspace_id` or `generation_id` from the environment. These are provided explicitly in the agent prompt. The prompt template must include:

```
WORKSPACE_ID: {workspace_id}
GENERATION_ID: {generation_id}
GITHUB_REPO: {github_repo}          # e.g. "org/repo-name"
GITHUB_REF: {github_ref}            # branch or SHA to deploy
DEPLOY_WORKFLOW: {workflow_file}    # e.g. "deploy.yml"
```

The agent must read these values from the prompt and use them as-is. It must **never** attempt to infer them by scanning the filesystem, reading environment variables, executing discovery commands, or inspecting git remotes.

---

## Allowed Tools

The agent is granted the following tools. The harness must configure these explicitly before the agent runs:

| Tool | Scope |
|------|-------|
| `Bash` | Allowed — all commands |
| `Python` | Allowed — all usage |
| `gh` | All subcommands allowed |
| `Read`, `Glob`, `Grep` | Workspace filesystem (read-only) |

Bash and Python are fully allowed for general use: running scripts, parsing output, local computation, file manipulation, etc.

The restriction is not on the tool — it is on the **intent**. Bash and Python must **not** be used to work around access errors: no direct cloud API calls (`curl` to GCP/AWS/Azure APIs, `boto3`, `google-cloud-*` SDKs, etc.) when the purpose is to substitute for a failing GHA auth step or extract credentials. If a deployment fails due to missing auth, the [failure protocol](#failure-protocol) applies — the agent stops and reports, it does not attempt to fix infra access from the workspace.

---

## What the Agent Does

### Deploy phase

1. Use `gh workflow run {DEPLOY_WORKFLOW} --repo {GITHUB_REPO} --ref {GITHUB_REF}` to trigger the deployment pipeline.
2. Poll `gh run list --repo {GITHUB_REPO} --workflow {DEPLOY_WORKFLOW}` to find the triggered run ID.
3. Poll `gh run view {run_id} --repo {GITHUB_REPO}` until the run reaches a terminal state (`completed`, `failure`, `cancelled`).
4. On success: proceed to QA phase.
5. On failure: execute the [failure protocol](#failure-protocol) below.

### QA phase

After a successful deploy, the agent runs smoke tests or checks against the deployed endpoint using the tools and instructions provided in the prompt. QA results are written to a designated output file in the workspace.

---

## GH Actions Log Access

The agent may attempt to read job logs via `gh run view --log` or `gh api`. This can fail with a **403 from Azure storage** — this is a known limitation of GitHub's log download in certain environments, not a configuration error.

When log download returns 403:
- The agent must **accept this as non-fatal** and continue.
- The agent must note the 403 in the failure report (see below).
- The agent must **not** retry with different auth headers, extract tokens, or attempt alternate storage access.
- Job failure reasons can often be inferred from step names and conclusions without full logs.

---

## Failure Protocol

When the deployment pipeline fails or the agent encounters an access error it cannot resolve, it must **stop immediately** and emit a structured failure report. The agent must **not** attempt workarounds.

### Prohibited workarounds

The following are strictly forbidden regardless of how close the agent thinks a fix might be:

- Reading, scanning, or dumping GitHub repository secrets
- Direct GCP IAM or API calls via `curl`, `python requests`, `gcloud`, or any SDK
- Attempting to mint, refresh, or extract tokens from any source
- Modifying GitHub Actions workflow files to bypass auth
- Retrying the same failing operation more than twice

### Failure report format

The agent must write the following to stdout and to `gain/deploy_failure_report.md` in the workspace:

```
DEPLOY FAILURE REPORT
=====================
generation_id: {generation_id}
workspace_id:  {workspace_id}
timestamp:     {ISO 8601 UTC}

FAILED STEP:   {step name from gh run view}
CONCLUSION:    {failure | cancelled | skipped}
LOG ACCESS:    {ok | 403 — logs unavailable}

ACCOUNTS PROVIDED TO AGENT:
  GitHub repo:       {GITHUB_REPO}
  Triggering ref:    {GITHUB_REF}
  GH CLI auth:       {gh auth status output}

WHAT IS MISSING / LIKELY CAUSE:
  {best-effort summary based on step name and conclusion}
  Example: "Step 'Authenticate to GCP via Workload Identity Federation' failed.
            GCP_WI_PROVIDER and/or GHA_SA GitHub secrets may not be configured,
            or the Workload Identity Federation binding is missing for this repo."

ACTION REQUIRED (human):
  {specific next steps for a human operator}
```

This report is consumed by the notification layer and forwarded verbatim to Slack and email. It must be human-readable and self-contained.

---

## Permission Configuration

The harness must grant the following before launching the agent. Missing any of these causes the agent to stall waiting for interactive permission approval, which hangs the pipeline.

```json
{
  "permissions": {
    "allow": [
      "Bash(*)",
      "Python(*)",
      "Bash(gh:*)"
    ]
  }
}
```

`Bash(*)` covers all shell commands including `gh`; `Bash(gh:*)` is the scoped form used in the harness `GH_CLI_USAGE` list. Either is valid here — the harness uses `Bash(gh:*)`. `Bash(*)` must be pre-approved so the agent never sees an interactive permission prompt during an unattended run.

---

## Known Issues from Initial Runs

| Issue | Root Cause | Fix |
|-------|-----------|-----|
| Agent spent time discovering `workspace_id`/`generation_id` | Values not injected into prompt | Always pass explicitly in prompt template |
| `gh` commands rejected as unauthorized tool | `gh` not listed in allowed tools | Add `Bash(gh:*)` to harness `GH_CLI_USAGE` / allow list |
| Agent stalled on Bash permission prompt | `Bash` not pre-approved | Pre-approve `Bash(*)` in harness config |
| Log download returned 403 | Azure storage auth not available in this environment | Accept as non-fatal; infer failure reason from step name |
| Agent attempted secret extraction and GCP IAM calls | No hard stop on access errors | Failure protocol: report and stop, no workarounds |
