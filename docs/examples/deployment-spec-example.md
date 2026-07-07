# Example: Deployment & Integration Testing Spec

Drop a file like this into your `specs/` directory (e.g. `specs/deployment.md`) to give
`check_specification_completeness` enough detail to lock **Part F — Integration & Deployment
Readiness** as `INTEGRATION_TESTS_READY`. Without it, Part F stays `LOCAL_ONLY` and the deploy/E2E
phase is skipped after code generation — you still get generated code, just no live deploy or
end-to-end run.

None of these fields are mandatory keywords — the analysis agent extracts them from whatever
prose or tables you provide. This is a worked example of what "locked" looks like.

```markdown
## Deployment

- **Deploy method**: GitHub Actions — `.github/workflows/deploy-dev.yml`
- **Target environment**: GKE cluster `gke-dev-1`, namespace `myapp-dev`
- **Image registry**: `ghcr.io/myorg/myapp`
- **Base URL**: `https://myapp-dev.internal`
- **Health check**: `GET /api/health`
- **E2E framework**: Playwright
- **E2E test location**: `e2e/`
- **Frontend API routing**: relative paths via reverse-proxy rewrites (no hostname baked into the frontend build)
- **Max QA rounds**: 3
- **Secret management**: GitHub Secrets only
- **Namespace pattern**: `{session_id}-{workspace_id}`
- **Teardown method**: `kubectl delete namespace` + `gcloud secrets delete`

## Manual Prerequisites (USER NOTICE)

- [ ] `gke-dev-1` cluster and `myapp-dev` namespace already exist
- [ ] `ghcr.io/myorg/myapp` registry access configured for the CI service account
- [ ] Required secrets (DB credentials, JWT signing key) already created in GitHub Secrets
```

## What if I don't provide any of this?

Then Part F is `LOCAL_ONLY` and the deploy/E2E phase is skipped after generation — this is the
default, and it's a fine choice if you just want generated code. The classification is
deterministic: it only becomes `INTEGRATION_TESTS_READY` once your spec describes **all three**
of deploy commands/workflow, acceptance/E2E test methodology, and infrastructure targets (cluster,
registry, namespace, or equivalent). Partial information keeps you at `LOCAL_ONLY`.

You don't, however, need to design your own CI/CD approach from scratch to satisfy the "deploy
method" dimension. SpecFlow's agent workspaces have no Docker daemon, `kubectl`, or cloud CLIs —
by design, every deploy always runs as a GitHub Actions workflow on a full-access runner
(build → push → deploy → E2E), regardless of what you write. That default mechanism is described
in [`backend/app/standards/deployment_standards.md`](../../backend/app/standards/deployment_standards.md).
So once you supply your infra targets and test methodology, you can reference "GitHub Actions" as
the deploy method rather than inventing a different pipeline. Anything that needs a human (cloud
resource provisioning, DNS, secrets, IAM) is called out explicitly in the analysis output as a
manual prerequisite rather than assumed.
