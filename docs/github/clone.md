---
type: Design
title: "GitHub clone check"
description: "The clone check of a project repository on the agents worker: the workflow, the token, the hardened shallow clone, the snapshot and its object keys"
status: stable
tags: [github, worker]
---
# clone.md: the clone check and the snapshot

The API starts `GithubSnapshotWorkflow` on the `agents` queue (id `github-snapshot-{repository_id}`, `USE_EXISTING`,
execution timeout 45 minutes) when a member adds a repository or asks for a new check. The input holds ids only
(`project_id`, `repository_id`, `id_prefix`): never a token or a repository name (`temporal.github.models`).

## Steps

1. Mark the row `cloning` (stop quietly when the repository is gone).
2. Get an access token of the row's `added_by` (`GitHubConnections.access_token`, refresh under the row lock). No
   `added_by` or no connection is `github_not_connected`; an expired one is `github_reconnect_required`. A worker
   without the `GITHUB_*` settings ends every check `github_auth_failed` (log `worker.github.snapshot.unconfigured`):
   a setup problem of the deployment, as a refused client secret is.
3. `GET /repositories/{github_repo_id}` with that token: not readable is `github_repo_not_accessible`; over 1 GiB is
   `github_repo_too_large`. The clone uses the `owner/name` of this answer (a renamed repository still clones); a name
   outside `owner/name` of `[A-Za-z0-9_.-]` is refused (`github_clone_failed`).
4. A shallow clone of the default branch into a temporary directory under `TMPDIR`, hardened (below); 10 minutes at
   most (`github_clone_timeout`).
5. Count the regular files of the working tree (no `.git`, links not followed), check the caps (below), and pack it as
   `tar.gz` (below) on the same disk, then stream the archive to object storage (`storage_failed` on any store error; the upload is
   aborted).
6. Mark the row `cloned` with the branch, the head SHA, the file count, the size and the snapshot; delete the snapshot
   it replaced. A repository removed meanwhile: delete the new snapshot.
7. Delete the temporary directory in every case. A failure marks the row `failed` with its code and keeps the last
   good snapshot.

## The git process

| Rule | How |
|---|---|
| The token never leaves the environment of the clone process | `GIT_CONFIG_COUNT/KEY/VALUE`: `http.https://github.com/.extraheader = AUTHORIZATION: basic base64(x-access-token:<token>)`; never argv, the URL, `.git/config`, a log line, an error or a workflow payload; `rev-parse` and `symbolic-ref` run without it |
| Only github.com over https | the remote is always `https://github.com/{owner}/{name}.git`; `protocol.allow=never`, `protocol.https.allow=always` |
| Nothing of the host or the worker | the environment is built from nothing: a fixed `PATH`, an empty private `HOME`/`XDG_CONFIG_HOME`, `GIT_CONFIG_NOSYSTEM=1`, `GIT_CONFIG_GLOBAL=/dev/null`, `LANG=C`; no worker secret reaches git |
| No prompts, hooks, helpers | `GIT_TERMINAL_PROMPT=0`, empty `GIT_ASKPASS`, `credential.helper=` (empty), `core.hooksPath=/dev/null`, `core.fsmonitor=false` |
| Small and bounded | `clone --depth 1 --single-branch --no-tags --no-recurse-submodules`, `GIT_LFS_SKIP_SMUDGE=1`; own process group, killed whole on the timeout or a cancellation |
| The token in the process environment | the clone process holds it in its environment, readable through `/proc/<pid>/environ` by the same uid while git runs. worker-agents runs nothing else as that uid today. **Agent tools must never run subprocesses in this container** (or this uid) without revisiting this rule |
| No GitHub text kept | stderr (at most 64 KiB kept) is read only to pick the code: "not found", "authentication failed", a 401/403/404 and the like are `github_repo_not_accessible`; a DNS, connect, TLS, 429 or 5xx failure is `github_unreachable`; anything else `github_clone_failed`. stderr is never logged or stored |

## The snapshot archive

`tar.gz` (PAX), deterministic: one clone of one commit gives the same bytes. Only regular files go in: members are
sorted by path, `mtime` 0, uid/gid 0, no user or group names, the gzip header has no name and `mtime` 0, and a file
keeps only the owner's execute bit (`0755` or `0644`). `.git`, devices, FIFOs and sockets are left out, and so is
**every symlink** (to a file or a directory, inside the tree or not): no chain of links (`p/q/a -> ../..` with
`p/q/s -> a/..`) can point a reader out of the tree. The number left out is logged as `links_skipped`. No member name
is absolute or has a `..` segment.

## Caps

| Cap | Where | Code |
|---|---|---|
| GitHub's `size` over 1 GiB | before the clone (the API lookup) | `github_repo_too_large` |
| The checked-out files over 1 GiB, or over 200,000 files | after the clone, before the archive (GitHub's `size` lags behind pushes) | `github_repo_too_large` |
| 10 minutes for the clone | `git clone` | `github_clone_timeout` |
| 64 KiB kept of each git pipe | the rest is read and dropped while git runs | - |

The checkout itself is not capped while git writes it: a repository that grew far past its `size` fills `TMPDIR`
until the timeout or the volume limit (on Kubernetes the `emptyDir` `sizeLimit` evicts the pod; the workflow then
ends the row `github_clone_failed`).

## Workflow, retries and the last resort

| Step | Policy |
|---|---|
| `github_snapshot` | start-to-close 20 minutes, heartbeat 2 minutes (a beat every 20 seconds), 2 attempts. A closed failure is recorded and returned, never retried. An unexpected error is retried once; on the last attempt it is recorded as `github_clone_failed` |
| `github_snapshot_fail` | runs when `github_snapshot` fails anyway (a crash or a timeout) or the workflow is cancelled: the row `failed` with `github_clone_failed`, so no row stays `cloning`; 5 attempts |

The workflow returns `{code, ended}` (`code` null on success; `ended` false when the repository was gone) and ends
`failed` (non-retryable `GitHubSnapshotFailed`) only after the last-resort activity ran.

## Disk

The clone and its archive live under `TMPDIR` at the same time: at most about twice the 1 GiB cap per check, plus a
retry. worker-agents sets `TMPDIR=/var/tmp/specflow`: a named volume in compose (`specflow-worker-agents-tmp`; the
256 MiB `/tmp` tmpfs is memory) and a 4 GiB disk `emptyDir` (`clones`) in the chart example. The image has `git` and
`ca-certificates` and owns `/var/tmp/specflow` (uid 10001).

## Log events

| Event | Fields |
|---|---|
| `worker.github.snapshot.started` | `project_id`, `repository_id` |
| `worker.github.snapshot.finished` | `project_id`, `repository_id`, `file_count`, `size_bytes`, `links_skipped`, `snapshot_bytes`, `duration_ms` |
| `worker.github.snapshot.failed` | `project_id`, `repository_id`, `code`, `duration_ms` |
| `worker.github.snapshot.gone` | `project_id`, `repository_id` (removed before the check started) |
| `worker.github.snapshot.unconfigured` | `project_id`, `repository_id` |
| `worker.github.snapshot.error` | `project_id`, `repository_id`, `error_type` (class name only), `last` |
| `worker.github.snapshot.delete_failed` | `project_id`, `repository_id`, `error_type` |

Never the token, the repository name, a GitHub text or git's stderr.

## Object keys

`projects/{project_id}/repositories/{repository_id}/{head_sha}.tar.gz` (`storage.keys.repository_snapshot`). One
snapshot per repository. Removing the repository deletes the prefix `projects/{project_id}/repositories/{repository_id}/`
(`storage.keys.repository_prefix`; `validate_prefix` accepts this shape).
