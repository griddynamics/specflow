// docs/github/INDEX.md: the GitHub tab's pieces rendered on the server, and its rules without React. The
// owner's connection row (connected, expired, none) and its access line, the repository rows in each clone state (badge, facts, failure
// reason, actions for owners only), the Add gate, the empty states, the repository picker (added, too large, no match, truncated,
// failed, none) and the `?github=` toasts and failure mapping. The clicks run in the production e2e.
import assert from "node:assert/strict";
import { before, test } from "node:test";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { stubCss } from "../support/css-stub.mjs";
import { DataError } from "@/lib/domain/errors";
import type { GitHubConnection, GitHubRepository, ProjectRepositories, ProjectRepository } from "@/lib/domain/github";
import type { AddDialogProps, RepositoriesProps } from "../../components/settings/github-parts";

stubCss();
async function load() {
  return {
    P: await import("../../components/settings/github-parts"),
    L: await import("@/components/shared/settings/github-logic"),
  };
}
let P: Awaited<ReturnType<typeof load>>["P"];
let L: Awaited<ReturnType<typeof load>>["L"];
before(async () => {
  ({ P, L } = await load());
});

const noop = () => undefined;
const conn = (over: Partial<GitHubConnection> = {}): GitHubConnection => ({
  enabled: true,
  connected: true,
  status: "active",
  login: "octo-dev",
  connectedAt: "2026-11-20T09:30:00Z",
  lastUsedAt: null,
  manageUrl: "https://github.com/settings/installations",
  ...over,
});
const NONE = conn({ connected: false, status: null, login: null, connectedAt: null });
const EXPIRED = conn({ status: "expired" });
const repo = (over: Partial<ProjectRepository> = {}): ProjectRepository => ({
  id: "r1",
  githubRepoId: 7,
  fullName: "acme/payments-api",
  htmlUrl: "https://github.com/acme/payments-api",
  private: true,
  addedByName: "Ann Lee",
  addedByMe: false,
  cloneState: "cloned",
  failureCode: null,
  defaultBranch: "main",
  headSha: "3f9c2e1d4b5a6978",
  fileCount: 1204,
  sizeBytes: 9_437_184,
  snapshotBytes: 3_355_443,
  checkedAt: "2026-11-21T10:00:00Z",
  clonedAt: "2026-11-21T10:00:00Z",
  createdAt: "2026-11-20T09:30:00Z",
  ...over,
});
const list = (repositories: ProjectRepository[], canManage = true): ProjectRepositories => ({ enabled: true, canManage, repositories });

const account = (c: GitHubConnection) =>
  renderToStaticMarkup(createElement(P.GitHubAccount, { connection: c, busy: false, onConnect: noop, onDisconnect: noop, onChoose: noop }));
const repos = (over: Partial<RepositoriesProps> = {}) =>
  renderToStaticMarkup(
    createElement(P.Repositories, {
      list: list([repo()]),
      row: { kind: "connected", login: "octo-dev" },
      busyIds: new Set<string>(),
      onAdd: noop,
      onClone: noop,
      onRemove: noop,
      ...over,
    }),
  );
const ghRepo = (id: number, fullName: string, over: Partial<GitHubRepository> = {}): GitHubRepository => ({
  githubRepoId: id,
  fullName,
  htmlUrl: `https://github.com/${fullName}`,
  private: false,
  defaultBranch: "main",
  sizeKb: 10,
  tooLarge: false,
  ...over,
});
const dialog = (over: Partial<AddDialogProps> = {}) =>
  renderToStaticMarkup(
    createElement(P.AddDialog, {
      state: { repositories: [ghRepo(1, "acme/web"), ghRepo(2, "acme/big", { tooLarge: true }), ghRepo(3, "acme/api")], truncated: false },
      query: "",
      addedIds: new Set([3]),
      adding: null,
      choosing: false,
      onQuery: noop,
      onAdd: noop,
      onRetry: noop,
      onChoose: noop,
      onClose: noop,
      ...over,
    }),
  );

test("connection row: connected shows the login, Disconnect and the access line; expired asks to reconnect; none to connect", () => {
  let view = account(conn());
  assert.match(view, /data-testid="github-row" data-state="connected"/);
  assert.match(view, /Connected to GitHub as octo-dev/);
  assert.match(view, /data-testid="github-disconnect"/);
  assert.match(view, /Choose repositories on GitHub/);
  assert.match(view, /href="https:\/\/github.com\/settings\/installations" target="_blank" rel="noopener noreferrer"/);
  view = account(EXPIRED);
  assert.match(view, /data-state="expired"/);
  assert.match(view, /Your GitHub connection expired/);
  assert.match(view, />Reconnect</);
  assert.doesNotMatch(view, /github-access/);
  view = account(NONE);
  assert.match(view, /data-state="none"/);
  assert.match(view, />Connect GitHub</);
  assert.doesNotMatch(view, /github-choose|github-manage/);
});

test("a cloned row: link, Private tag, branch and who added it, short SHA, files and snapshot size; owners get Clone again and Remove", () => {
  const view = repos();
  assert.match(view, /href="https:\/\/github.com\/acme\/payments-api"/);
  assert.match(view, />Private</);
  assert.match(view, /main · Added by Ann Lee/);
  assert.match(view, /3f9c2e1 · 1,204 files · 3.2 MB/);
  assert.match(view, />Cloned</);
  assert.match(view, /data-testid="github-clone"/);
  assert.match(view, /aria-label="Remove acme\/payments-api"/);
  assert.match(view, /data-testid="github-add"/);
  assert.match(view, /data-testid="github-count">1</);
});

test("pending and cloning rows show their badge and disable Clone again; a failed row names the reason and keeps the last good copy", () => {
  let view = repos({
    list: list([repo({ cloneState: "pending", headSha: null, fileCount: null, sizeBytes: null, snapshotBytes: null })]),
  });
  assert.match(view, />Waiting</);
  assert.match(view, /<button[^>]*disabled=""[^>]*data-testid="github-clone"/);
  assert.doesNotMatch(view, /github-repo-facts/);
  assert.match(repos({ list: list([repo({ cloneState: "cloning" })]) }), />Cloning</);
  view = repos({ list: list([repo({ cloneState: "failed", failureCode: "github_repo_not_accessible" })]) });
  assert.match(view, />Failed</);
  assert.match(view, /Last good copy: 3f9c2e1/);
  assert.match(view, /GitHub refused access/);
  view = repos({ list: list([repo({ cloneState: "failed", failureCode: "brand_new_code", headSha: null })]) });
  assert.match(view, /The clone check failed\./);
});

test("a member sees the list read-only: no Add, no Clone again, no Remove; the empty state speaks to each role", () => {
  const view = repos({ list: list([repo()], false), row: null });
  assert.doesNotMatch(view, /github-add|github-clone|github-remove/);
  assert.match(view, />Cloned</);
  assert.match(repos({ list: list([], false), row: null }), /An owner adds the project&#x27;s GitHub repositories here\./);
  assert.match(repos({ list: list([]) }), /Add a GitHub repository so Specflow can read the project&#x27;s code\./);
});

test("Add repository is gated on an active connection, with the reason", () => {
  const view = repos({ row: { kind: "none" } });
  assert.match(view, /<button[^>]*disabled=""[^>]*data-testid="github-add"/);
  assert.match(view, /Connect GitHub to add repositories\./);
  assert.match(repos({ row: { kind: "expired" } }), /Reconnect GitHub to add repositories\./);
  assert.doesNotMatch(repos(), /github-gate/);
});

test("the picker: addable rows first with Add, an added one tagged Added, a too large one tagged; search filters; truncated is said", () => {
  const view = dialog();
  const order = [...view.matchAll(/<b title="([^"]+)"/g)].map((m) => m[1]);
  assert.deepEqual(order, ["acme/web", "acme/big", "acme/api"]);
  assert.match(view, /aria-label="Add acme\/web"/);
  assert.match(view, /data-blocked="too_large"[\s\S]*Over 1 GB/);
  assert.match(view, /data-blocked="added"[\s\S]*>Added</);
  assert.doesNotMatch(dialog({ query: "WEB" }), /acme\/api/);
  assert.match(dialog({ query: "zzz" }), /No matching repository/);
  assert.match(dialog({ state: { repositories: [ghRepo(1, "a/b")], truncated: true } }), /Showing the first 1,000 repositories\./);
  assert.match(dialog({ adding: 1 }), /<button[^>]*disabled=""[^>]*data-testid="github-pick-add"/);
});

test("the picker without repositories points to GitHub; loading and failed have their own states", () => {
  const view = dialog({ state: { repositories: [], truncated: false } });
  assert.match(view, /No repositories to add/);
  assert.match(view, /data-testid="github-pick-choose"/);
  assert.match(dialog({ state: "loading" }), /data-testid="github-pick-loading"/);
  assert.match(dialog({ state: "failed" }), /Your repositories could not be loaded\./);
});

test("GitHub off: one note", () => {
  assert.match(renderToStaticMarkup(createElement(P.GitHubUnavailable)), /GitHub is not set up on this Specflow server/);
});

test("rules: bytes, SHA and file texts; isCloning; withRepository replaces or prepends", () => {
  assert.equal(L.bytesText(512), "512 B");
  assert.equal(L.bytesText(1536), "1.5 KB");
  assert.equal(L.bytesText(20 * 1024 * 1024), "20 MB");
  assert.equal(L.bytesText(3 * 1024 ** 4), "3072 GB");
  assert.equal(L.shortSha("abcdef1234"), "abcdef1");
  assert.equal(L.filesText(1), "1 file");
  assert.equal(L.isCloning(list([repo()])), false);
  assert.equal(L.isCloning(list([repo(), repo({ id: "r2", cloneState: "cloning" })])), true);
  assert.equal(L.isCloning(null), false);
  const two = L.withRepository(list([repo()]), repo({ id: "r2" }));
  assert.deepEqual(
    two.repositories.map((r) => r.id),
    ["r2", "r1"],
  );
  assert.equal(L.withRepository(two, repo({ cloneState: "pending" })).repositories[1].cloneState, "pending");
});

test("?github= toasts: connected and installed succeed, the known codes have their text, anything else is the generic failure", () => {
  assert.deepEqual(L.githubToast("connected"), { title: "GitHub connected", tone: "success" });
  assert.equal(L.githubToast("installed").tone, "success");
  assert.equal(L.githubToast("github_consent_denied").title, "GitHub access was not granted.");
  assert.equal(L.githubToast("github_state_invalid").title, "The GitHub sign-in expired. Try again.");
  assert.equal(L.githubToast("github_auth_failed").tone, "danger");
  assert.match(L.githubToast("github_auth_failed").title, /GitHub refused the request\./);
  assert.deepEqual(L.githubToast("<script>"), { title: "Could not connect GitHub. Try again.", tone: "danger" });
});

test("failure mapping: a gone project goes home; connection and list changes reload; others are a toast", () => {
  const err = (apiCode: string) => new DataError("conflict", false, { status: 409, apiCode });
  assert.deepEqual(L.githubFailureOf(err("project_not_found")), { kind: "gone" });
  assert.equal(L.githubFailureOf(err("github_not_connected")).kind, "reload");
  assert.equal(L.githubFailureOf(err("github_reconnect_required")).kind, "reload");
  assert.equal(L.githubFailureOf(err("repository_exists")).kind, "reload");
  assert.deepEqual(L.githubFailureOf(err("github_repo_too_large")), {
    kind: "toast",
    text: "This repository is larger than 1 GB. Specflow cannot copy it.",
  });
  assert.equal(L.githubFailureOf(err("github_auth_failed")).kind, "toast");
  assert.match(L.failureText("github_auth_failed"), /GitHub refused the clone\./);
  assert.deepEqual(L.githubFailureOf(new Error("x")), { kind: "toast", text: "Something went wrong. Try again." });
});
