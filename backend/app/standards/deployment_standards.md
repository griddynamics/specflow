# Deployment Standards — Overview

## Default Architecture

SpecFlow agents run on GKE pods with no Docker daemon and no cloud CLIs. All building, deployment,
and testing is delegated to GitHub Actions workflows, which run on runners with full cloud access.

```
Agent writes code → git push → gh workflow run deploy.yml
                                        ↓
                              GitHub Actions runner
                              (Docker, cloud CLIs, K8s/cloud access)
                                        ↓
                              Build image → Push to registry → Deploy → Run E2E tests
                                        ↓
Agent reads results ← gh run view --log
```

### Tools NOT available on SpecFlow workspaces

Agents cannot run any of the following directly — delegate all of these to GHA workflow steps:

| Absent tool | Reason |
|-------------|--------|
| `docker` / `docker-compose` | No Docker daemon on GKE pods |
| `kubectl`, `helm` | No K8s credentials or kubeconfig |
| `aws`, `gcloud`, `az` | No cloud CLIs installed |
| Direct network access to target infra | Pods have no route to customer environments |

Key constraints:
- Agents use `gh` CLI to trigger workflows and read logs — never `docker`, `kubectl`, or cloud CLIs directly.
- E2E tests run inside the deploy pipeline (same GHA runner/job), not as a separate workflow hitting a public URL.
- Deploy is triggered only in the QA_LOOP phase — never during code generation.
- In GitHub Actions workflows, always pin each action to its latest stable major version (`actions/checkout@v4`, `actions/setup-java@v4`, `actions/cache@v4`, `actions/upload-artifact@v4`, `actions/download-artifact@v4`) — never `@v3`, which GitHub hard-deprecated and auto-fails before the run starts.

## How Agents Trigger and Monitor Deployments

```bash
# 1. Push latest code (Dockerfile, GHA workflows, k8s manifests, test scripts)
git add -A && git commit -m "infrastructure: update deployment config" && git push

# 2. Trigger the unified deploy-and-test workflow
gh workflow run deploy.yml \
  --ref "$(git branch --show-current)" \
  -f run_e2e=true \
  -f generation_id="$GENERATION_ID" \
  -f workspace_id="$WORKSPACE_ID"

# 3. Poll for completion — NEVER use `gh run watch` (it never exits; violates no-blocking-process rule)
while true; do
  STATUS=$(gh run list --workflow=deploy.yml --limit=1 --json status -q '.[0].status')
  if [ "$STATUS" != "in_progress" ] && [ "$STATUS" != "queued" ]; then break; fi
  sleep 30
done

# 4. Read results
RUN_ID=$(gh run list --workflow=deploy.yml --limit=1 --json databaseId -q '.[0].databaseId')
gh run view "$RUN_ID" --log        # full output
gh run view "$RUN_ID" --log-failed # failed steps only
```

Max 3 deploy-test cycles per QA round — do not retry indefinitely.

## Mobile (Android) Builds & E2E

The same delegation rule applies, split by what the pod can and cannot do:

For JVM hang causes and Gradle one-shot command rules, read `./standards/jvm_hanging_tests.md`.

| Task | Where it runs | Why |
|------|---------------|-----|
| Compile Kotlin/Java, `./gradlew assembleDebug` / `bundleRelease`, NDK/CMake builds, unit + Robolectric tests | In-pod | JDK 21 + the shared Android SDK (`platforms`, `build-tools`, `cmake`, `ndk`) are present. Pure CPU/IO, no virtualization. |
| Anything needing a running Android emulator — instrumented tests (`connectedAndroidTest`, Espresso/Compose UI), Maestro flows, screenshot/UI E2E | GHA only | Delegate exactly like deploy. Never start an emulator in the pod. |

Why the emulator must not run in the pod:
- The pod has no `/dev/kvm` — without hardware acceleration the emulator is unusably slow and flaky (multi-minute boots, ANR/timeouts).
- Exposing `/dev/kvm` needs a privileged container, which breaks the agent sandbox boundary.
- An emulator's RAM/CPU footprint risks OOM-killing the single shared pod, destroying all in-flight runs.

GHA pattern for Android E2E — add to the same `deploy.yml` pipeline, on `ubuntu-latest`:

**Required:** grant the runner user KVM access *before* `reactivecircus/android-emulator-runner` (documented prerequisite in that action's README). GitHub-hosted Ubuntu runners have `/dev/kvm`, but the runner user is not in the `kvm` group by default. Skipping this step forces TCG + swiftshader software emulation — boot exceeds typical timeouts (600s), adb stays offline/empty.

```yaml
- name: Enable KVM group perms
  run: |
    echo 'KERNEL=="kvm", GROUP="kvm", MODE="0660", OPTIONS+="static_node=kvm"' | sudo tee /etc/udev/rules.d/99-kvm4all.rules
    sudo udevadm control --reload-rules
    sudo udevadm trigger --name-match=kvm
- name: Android E2E
  uses: reactivecircus/android-emulator-runner@v2
  with:
    api-level: 34
    arch: x86_64
    # default target (not google_apis) unless Play/APIs are required — boots faster
    # cores: 4 if runner has headroom; 2-core TCG without KVM is especially slow
    script: ./gradlew connectedAndroidTest   # or: maestro test .maestro/
```

UI driving — there is no mobile equivalent of the Playwright MCP. Do not expect an
interactive "drive the emulator UI" tool on the workspace. Instead the agent authors
instrumented UI tests (Espresso / Compose UI Test) or Maestro flow YAML committed to the
repo, and the GHA job executes them against the emulator. The `android-emulator-runner`
action owns emulator boot and teardown, so no emulator process leaks back to the runner.

## iOS / macOS — NOT supported on this host

SpecFlow hosted agents run on Linux. iOS and macOS targets require macOS + Xcode, which cannot run
on Linux and cannot be containerised (Apple licensing; no Linux-kernel macOS). Do not
attempt any of the following — they will always fail, so do not generate, run, or "try" them:

- `xcodebuild`, `xcrun`, `simctl`, CocoaPods/`pod`, `swift build`, Fastlane iOS lanes
- Booting an iOS Simulator or building `.ipa` / `.app` / Swift/Objective-C targets
- Installing Xcode or the iOS SDK into the workspace or any GHA Linux runner

This is a hard host limitation, not a missing dependency — there is no flag, package, or
emulator that makes iOS buildable on Linux. Installing the Android SDK does not enable it.

What to do instead:
- If the spec targets iOS/macOS, treat it as a USER NOTICE (Part F): iOS build and
  simulator E2E must run on macOS — either GitHub `macos-latest` runners (Xcode + iOS
  Simulator; the Simulator needs no KVM) or a self-hosted Mac runner. Author the tests
  (XCUITest / Maestro), commit them, and delegate execution to that macOS runner exactly as
  Android emulator E2E is delegated above.
- For cross-platform stacks (Flutter, React Native, KMP), build and test the Android side
  here per the section above, and flag the iOS side as the macOS-runner USER NOTICE.


## Default Artifacts Every Project Generates

When the specification does not specify deployment, agents produce:

| Artifact | Purpose |
|----------|---------|
| `Dockerfile` | Multi-stage build for the application |
| `docker-compose.yml` | End-user local development only (agents cannot execute it) |
| `.github/workflows/deploy.yml` | `workflow_dispatch` trigger; single pipeline: build → push → deploy → E2E test |
| `.github/workflows/teardown.yml` | `workflow_dispatch` trigger; single pipeline: teardown all added resources and deployments |
| `.dockerignore` | Excludes build artifacts, secrets, test files |
| Health check endpoint | `GET /api/health` returning 200 |

## Authentication: Workload Identity Federation (WIF)

Agents use keyless auth (OIDC/WIF) in GHA workflows. WIF must be pre-configured by the platform
team for each repository before any deploy workflow can authenticate to GCP/AWS/Azure.
You - coding agent - can't modify this setup. If its not working, document that as instructed and exit.

- GCP: per-repo WIF provider in the `github` pool + `workloadIdentityUser` binding for `gha-sa`
- AWS: per-repo IAM role `specflow-gha-{REPO_SLUG}` with OIDC trust policy
- Azure: federated credentials on the App Registration scoped to the repository

WIF failures surface as 403/`invalid_target` in GHA logs — these are infrastructure gaps, not code
bugs. Stop the deploy loop and escalate to the platform team.

## Operations Requiring Manual User Action (USER NOTICE)

The following always require operator action before the DEPLOY phase can succeed:

| Operation | Why |
|-----------|-----|
| Cloud resource provisioning (cluster, registry, DB) | Requires cloud admin permissions |
| WIF / OIDC trust registration per repository | Requires IAM admin |
| GitHub secrets configuration | Requires repo admin access |
| DNS / TLS certificate setup | Requires domain/cert admin |
| VPC / firewall / network config | Requires network admin |

Flag any of the above detected from the spec as USER NOTICE items in Part F.
