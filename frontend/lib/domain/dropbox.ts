// docs/dropbox/oauth.md "Routes": the caller's Dropbox connection. Screens reach it only through `DropboxPort`; the http adapter (`lib/data/http/dropbox.ts`) is the one
// implementation. Binding the connection to a project is `IntegrationsPort`; the Dropbox link source is `SourcesPort` (`checkDropbox`, `addDropbox`).

/** `GET /integrations/dropbox`: the caller's own connection. `enabled` false = Dropbox is not configured on this server. */
export interface DropboxConnection {
  enabled: boolean;
  connected: boolean;
  status: "active" | "expired" | null;
  /** The Dropbox display name (the e-mail address of the account is not kept). */
  name: string | null;
  /** The team of a Dropbox Business member; null for a personal account. */
  teamName: string | null;
  connectedAt: string | null;
  lastUsedAt: string | null;
}

export interface DropboxPort {
  /** The caller's Dropbox connection (answers also when Dropbox is off). */
  connection(): Promise<DropboxConnection>;
  /** The Dropbox sign-in URL; the browser returns to `returnTo` (a same-site path) through the callback route with `?dropbox=<code>`. */
  authorize(returnTo: string): Promise<string>;
  /** Forgets the caller's Dropbox connection (and revokes the grant at Dropbox). */
  disconnect(): Promise<void>;
}
