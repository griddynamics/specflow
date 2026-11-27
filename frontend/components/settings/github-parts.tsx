"use client";
// docs/github/INDEX.md: the pieces of the GitHub tab. Each takes plain props and has no data access;
// GitHubTab wires them to the port. Design-system components only: Button, Tag, Tooltip, Icon, GlassSurface, EmptyState,
// Skeleton, Modal, TextField; the account row and the confirmation are account-parts.tsx.
import { markFor } from "@/components/connections/logos";
import { Button } from "@/components/ui/Button";
import { EmptyState } from "@/components/ui/EmptyState";
import { GlassSurface } from "@/components/shared/ui/GlassSurface";
import { Icon } from "@/components/shared/ui/Icon";
import { Modal } from "@/components/ui/Modal";
import { Skeleton } from "@/components/ui/Skeleton";
import { Tag } from "@/components/ui/Tag";
import { TextField } from "@/components/ui/TextField";
import { Tooltip } from "@/components/ui/Tooltip";
import { COPY } from "@/lib/copy";
import type { GitHubConnection, GitHubRepositoryList, ProjectRepositories, ProjectRepository } from "@/lib/domain/github";
import { AccountRow, whenText } from "./account-parts";
import { addedByText, addGate, cloneViewOf, githubRowOf, pickRowsOf, type GitHubRow, type PickRow } from "@/components/shared/settings/github-logic";
import { SETTINGS_COPY } from "@/components/shared/settings/settings-copy";
import "./settings.css";

// GitHub's mark is near-black: it is drawn in the ink colour (GitHub's brand allows a one-colour mark), so it shows in both themes.
const GITHUB_PATH = markFor("github")?.path;
const GITHUB_MARK = GITHUB_PATH === undefined ? undefined : { path: GITHUB_PATH };

/** GitHub is not configured on this server: one note instead of the tab. */
export function GitHubUnavailable() {
  return (
    <p className="settings-note" role="note" data-testid="github-unavailable">
      <Icon name="plug" size={20} />
      {COPY.githubUnavailable}
    </p>
  );
}

function rowTexts(row: GitHubRow, connectedAt: string | null): { title: string; hint: string } {
  if (row.kind === "connected") {
    return {
      title: `${SETTINGS_COPY.githubConnectedAs} ${row.login}`,
      hint: connectedAt === null ? "" : `${SETTINGS_COPY.githubConnectedOn} ${whenText(connectedAt)}`,
    };
  }
  if (row.kind === "expired") return { title: SETTINGS_COPY.githubExpired, hint: SETTINGS_COPY.githubExpiredHint };
  return { title: SETTINGS_COPY.githubNone, hint: SETTINGS_COPY.githubNoneHint };
}

export interface GitHubAccountProps {
  connection: GitHubConnection;
  /** Connect, Disconnect or Choose repositories is in flight. */
  busy: boolean;
  onConnect: () => void;
  onDisconnect: () => void;
  onChoose: () => void;
}

/** The owner's own GitHub connection, and while connected the way to pick the repositories the Specflow app may read. */
export function GitHubAccount(p: Readonly<GitHubAccountProps>) {
  const row = githubRowOf(p.connection);
  const { title, hint } = rowTexts(row, p.connection.connectedAt);
  return (
    <AccountRow
      state={row.kind}
      label={SETTINGS_COPY.githubLabel}
      title={title}
      hint={hint}
      markSrc={GITHUB_MARK}
      connectText={SETTINGS_COPY.githubConnect}
      reconnectText={SETTINGS_COPY.githubReconnect}
      disconnectText={SETTINGS_COPY.githubDisconnect}
      testId="github"
      busy={p.busy}
      onConnect={p.onConnect}
      onDisconnect={p.onDisconnect}
    >
      {row.kind === "connected" ? (
        <div className="settings-github-access" data-testid="github-access">
          <span className="settings-hint">{SETTINGS_COPY.githubAccessHint}</span>
          <span className="settings-github-access__actions">
            <Button variant="glass" size="sm" disabled={p.busy} onClick={p.onChoose} data-testid="github-choose">
              {SETTINGS_COPY.githubChoose}
            </Button>
            {p.connection.manageUrl === null ? null : (
              <a
                className="settings-link"
                href={p.connection.manageUrl}
                target="_blank"
                rel="noopener noreferrer"
                data-testid="github-manage"
              >
                {SETTINGS_COPY.githubManage}
              </a>
            )}
          </span>
        </div>
      ) : null}
    </AccountRow>
  );
}

/** One repository of the project: its name and link, who added it, the clone badge, the snapshot facts or the failure reason. */
function RepositoryRow({
  repo,
  busy,
  onClone,
  onRemove,
}: Readonly<{ repo: ProjectRepository; busy: boolean; onClone?: () => void; onRemove?: () => void }>) {
  const view = cloneViewOf(repo);
  const meta = [repo.defaultBranch, addedByText(repo)].filter((t): t is string => t !== null && t !== "").join(" · ");
  const running = repo.cloneState === "pending" || repo.cloneState === "cloning";
  return (
    <li className="settings-row settings-repo" data-testid="github-repo" data-state={repo.cloneState}>
      <div className="settings-who settings-repo__who">
        <span className="settings-repo__name">
          <a className="settings-link" href={repo.htmlUrl} target="_blank" rel="noopener noreferrer" title={repo.fullName}>
            {repo.fullName}
          </a>
          {repo.private ? <Tag>{SETTINGS_COPY.private}</Tag> : null}
        </span>
        {meta === "" ? null : <small>{meta}</small>}
        {view.facts === "" ? null : (
          <small className="settings-repo__facts" data-testid="github-repo-facts">
            {view.facts}
            {repo.checkedAt === null ? "" : ` · ${whenText(repo.checkedAt)}`}
          </small>
        )}
        {view.reason === null ? null : (
          <small className="settings-repo__reason" data-testid="github-repo-reason">
            <Icon name="warning-circle" size={16} />
            {view.reason}
          </small>
        )}
      </div>
      <span className="settings-repo__end">
        <Tag tone={view.tone}>{view.word}</Tag>
        {onClone === undefined ? null : (
          <Button
            variant="glass"
            size="sm"
            disabled={busy || running}
            onClick={onClone}
            aria-label={`${SETTINGS_COPY.cloneAgain} ${repo.fullName}`}
            data-testid="github-clone"
          >
            {SETTINGS_COPY.cloneAgain}
          </Button>
        )}
        {onRemove === undefined ? null : (
          <Tooltip label={SETTINGS_COPY.removeRepo} side="left">
            <button
              type="button"
              className="gdg-iconbtn"
              aria-label={`${SETTINGS_COPY.removeRepo} ${repo.fullName}`}
              disabled={busy}
              data-testid="github-remove"
              onClick={onRemove}
            >
              <Icon name="x" size={16} />
            </button>
          </Tooltip>
        )}
      </span>
    </li>
  );
}

export interface RepositoriesProps {
  list: ProjectRepositories;
  /** The caller's connection row; null = not an owner (the list is read-only). */
  row: GitHubRow | null;
  /** Repository ids with a Clone again or Remove in flight. */
  busyIds: ReadonlySet<string>;
  onAdd: () => void;
  onClone: (repo: ProjectRepository) => void;
  onRemove: (repo: ProjectRepository) => void;
}

/** The project's repositories: the head with the count and Add repository (owners), the rows or the empty state. */
export function Repositories(p: Readonly<RepositoriesProps>) {
  const row = p.list.canManage ? p.row : null; // null: a member, or not read
  const owner = row !== null;
  const gate = row === null ? null : addGate(row);
  const count = p.list.repositories.length;
  return (
    <section className="settings-sec" aria-label={SETTINGS_COPY.reposLabel} data-testid="github-repos">
      <div className="settings-sec__head">
        <span className="settings-repos__title">
          <h2 className="title-2 settings-sec__title">{SETTINGS_COPY.reposTitle}</h2>
          {count === 0 ? null : (
            <span className="settings-count" data-testid="github-count">
              {String(count)}
            </span>
          )}
        </span>
        {owner ? (
          <Button
            size="sm"
            variant={gate === null ? "primary" : "glass"}
            disabled={gate !== null}
            onClick={p.onAdd}
            data-testid="github-add"
          >
            {SETTINGS_COPY.addRepo}
          </Button>
        ) : null}
      </div>
      {gate === null ? null : (
        <p className="settings-hint" data-testid="github-gate">
          {gate}
        </p>
      )}
      {count === 0 ? (
        <EmptyState title={SETTINGS_COPY.reposEmpty} icon="git-branch" variant="regular">
          {owner ? SETTINGS_COPY.reposEmptyOwner : SETTINGS_COPY.reposEmptyMember}
        </EmptyState>
      ) : (
        <GlassSurface as="ul" className="settings-list" data-testid="github-repo-list">
          {p.list.repositories.map((repo) => (
            <RepositoryRow
              key={repo.id}
              repo={repo}
              busy={p.busyIds.has(repo.id)}
              onClone={
                owner
                  ? () => {
                      p.onClone(repo);
                    }
                  : undefined
              }
              onRemove={
                owner
                  ? () => {
                      p.onRemove(repo);
                    }
                  : undefined
              }
            />
          ))}
        </GlassSurface>
      )}
    </section>
  );
}

function PickRowView({ row, adding, onAdd }: Readonly<{ row: PickRow; adding: number | null; onAdd: (id: number) => void }>) {
  const { repo } = row;
  let end;
  if (row.blocked === "added") end = <Tag tone="success">{SETTINGS_COPY.added}</Tag>;
  else if (row.blocked === "too_large") end = <Tag tone="warning">{SETTINGS_COPY.tooLarge}</Tag>;
  else {
    end = (
      <Button
        variant="glass"
        size="sm"
        disabled={adding !== null}
        onClick={() => {
          onAdd(repo.githubRepoId);
        }}
        aria-label={`${SETTINGS_COPY.addOne} ${repo.fullName}`}
        data-testid="github-pick-add"
      >
        {SETTINGS_COPY.addOne}
      </Button>
    );
  }
  return (
    <li className="settings-row" data-testid="github-pick" data-blocked={row.blocked ?? "none"}>
      <div className="settings-who">
        <b title={repo.fullName}>{repo.fullName}</b>
        <small>{[repo.private ? SETTINGS_COPY.private : null, repo.defaultBranch].filter((t) => t !== null).join(" · ")}</small>
      </div>
      {end}
    </li>
  );
}

export interface AddDialogProps {
  /** The caller's repositories, `loading` or `failed`. */
  state: GitHubRepositoryList | "loading" | "failed";
  query: string;
  addedIds: ReadonlySet<number>;
  /** The GitHub id being added, or null. */
  adding: number | null;
  /** Choose repositories on GitHub is in flight. */
  choosing: boolean;
  onQuery: (q: string) => void;
  onAdd: (githubRepoId: number) => void;
  onRetry: () => void;
  onChoose: () => void;
  onClose: () => void;
}

function AddBody(p: Readonly<AddDialogProps>) {
  if (p.state === "loading") {
    return (
      <div aria-busy="true" data-testid="github-pick-loading">
        <Skeleton variant="block" height={160} />
      </div>
    );
  }
  if (p.state === "failed") {
    return (
      <div className="settings-note" role="alert" data-testid="github-pick-failed">
        <span>{SETTINGS_COPY.addLoadFailed}</span>
        <Button variant="glass" size="sm" onClick={p.onRetry}>
          {SETTINGS_COPY.tryAgain}
        </Button>
      </div>
    );
  }
  if (p.state.repositories.length === 0) {
    return (
      <EmptyState
        title={SETTINGS_COPY.addNone}
        icon="git-branch"
        variant="regular"
        actions={
          <Button variant="glass" size="sm" disabled={p.choosing} onClick={p.onChoose} data-testid="github-pick-choose">
            {SETTINGS_COPY.githubChoose}
          </Button>
        }
      >
        {SETTINGS_COPY.addNoneBody}
      </EmptyState>
    );
  }
  const rows = pickRowsOf(p.state.repositories, p.query, p.addedIds);
  return (
    <div className="settings-pick">
      <TextField
        label={SETTINGS_COPY.addSearch}
        type="search"
        value={p.query}
        onChange={(e) => {
          p.onQuery(e.target.value);
        }}
      />
      {rows.length === 0 ? (
        <EmptyState title={SETTINGS_COPY.addNoMatch} icon="magnifying-glass" variant="regular">
          {SETTINGS_COPY.addNoMatchBody}
        </EmptyState>
      ) : (
        <ul className="settings-list settings-pick__list" data-testid="github-pick-list">
          {rows.map((row) => (
            <PickRowView key={row.repo.githubRepoId} row={row} adding={p.adding} onAdd={p.onAdd} />
          ))}
        </ul>
      )}
      {p.state.truncated ? (
        <p className="settings-hint" data-testid="github-pick-truncated">
          {SETTINGS_COPY.addTruncated}
        </p>
      ) : null}
    </div>
  );
}

/** The repository picker: a search over the caller's repositories; each can be added once, a too large one not at all. */
export function AddDialog(p: Readonly<AddDialogProps>) {
  return (
    <Modal title={SETTINGS_COPY.addTitle} onClose={p.onClose} closeDisabled={p.adding !== null}>
      <AddBody {...p} />
    </Modal>
  );
}
