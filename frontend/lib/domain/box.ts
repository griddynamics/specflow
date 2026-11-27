// docs/box/oauth.md "Routes": the caller's Box connection. Screens reach it only through `BoxPort`; the http adapter (`lib/data/http/box.ts`) is the one
// implementation. Binding the connection to a project is `IntegrationsPort`; the Box link source is `SourcesPort` (`checkBox`, `addBox`).

/** `GET /integrations/box`: the caller's own connection. `enabled` false = Box is not configured on this server. */
export interface BoxConnection {
  enabled: boolean;
  connected: boolean;
  status: "active" | "expired" | null;
  /** The Box display name (the e-mail address of the account is not kept). */
  name: string | null;
  /** The enterprise; null for a personal account. */
  enterpriseName: string | null;
  connectedAt: string | null;
  lastUsedAt: string | null;
}

export interface BoxPort {
  /** The caller's Box connection (answers also when Box is off). */
  connection(): Promise<BoxConnection>;
  /** The Box sign-in URL; the browser returns to `returnTo` (a same-site path) through the callback route with `?box=<code>`. */
  authorize(returnTo: string): Promise<string>;
  /** Forgets the caller's Box connection (and revokes the grant at Box). */
  disconnect(): Promise<void>;
}
