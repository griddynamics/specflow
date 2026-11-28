// docs/github/api.md: the caller's GitHub connection, the repositories that connection can read, and the GitHub
// repositories of a project with their clone check. Screens reach them only through `GitHubPort`; the http adapter
// (`lib/data/http/github.ts`) is the one implementation.

/** `GET /integrations/github`: the caller's own connection. `enabled` false = GitHub is not configured on this server. */
export interface GitHubConnection {
  enabled: boolean;
  connected: boolean;
  status: "active" | "expired" | null;
  login: string | null;
  connectedAt: string | null;
  lastUsedAt: string | null;
  /** GitHub's page of the user's app installations (where repositories are added or removed); null when not enabled. */
  manageUrl: string | null;
}

/** A repository the caller's connection can read through the Specflow GitHub App. `tooLarge`: over the clone size cap. */
export interface GitHubRepository {
  githubRepoId: number;
  fullName: string;
  htmlUrl: string;
  private: boolean;
  defaultBranch: string | null;
  sizeKb: number;
  tooLarge: boolean;
}

/** `GET /integrations/github/repositories`; `truncated`: the list stopped at its cap. */
export interface GitHubRepositoryList {
  repositories: GitHubRepository[];
  truncated: boolean;
}

export type CloneState = "pending" | "cloning" | "cloned" | "failed";

/** A repository of a project and its last clone check. The `cloned*` facts stay from the last good snapshot after a later failure. */
export interface ProjectRepository {
  id: string;
  githubRepoId: number;
  fullName: string;
  htmlUrl: string;
  private: boolean;
  addedByName: string | null;
  addedByMe: boolean;
  cloneState: CloneState;
  failureCode: string | null;
  defaultBranch: string | null;
  headSha: string | null;
  fileCount: number | null;
  sizeBytes: number | null;
  snapshotBytes: number | null;
  checkedAt: string | null;
  clonedAt: string | null;
  createdAt: string;
}

/** `GET /projects/{pid}/repositories`: `canManage` = the caller is an owner. */
export interface ProjectRepositories {
  enabled: boolean;
  canManage: boolean;
  repositories: ProjectRepository[];
}

export interface GitHubPort {
  /** The caller's GitHub connection (answers also when GitHub is off). */
  connection(): Promise<GitHubConnection>;
  /** The GitHub authorize URL; the browser returns to `returnTo` (a same-site path) through the callback route. */
  authorize(returnTo: string): Promise<string>;
  /** The URL that installs the Specflow GitHub App (or picks its repositories); the browser returns through the setup route. */
  install(returnTo: string): Promise<string>;
  /** The repositories the caller's connection can read. */
  available(): Promise<GitHubRepositoryList>;
  list(projectId: string): Promise<ProjectRepositories>;
  add(projectId: string, githubRepoId: number): Promise<ProjectRepository>; // owner only; starts the clone check
  remove(projectId: string, repositoryId: string): Promise<void>; // owner only
}
