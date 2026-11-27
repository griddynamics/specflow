// docs/notion/oauth.md "Routes": the caller's own Notion connection. Screens reach it only through `NotionConnectionPort`; the http adapter
// (`lib/data/http/notion-connection.ts`) is the one implementation. The OAuth callback is not here: the route handler
// `app/api/integrations/notion/callback` calls the API on the server.

/** `GET /integrations/notion`. `enabled` false = Notion is off on the server (everything else is empty). */
export interface NotionConnection {
  enabled: boolean;
  connected: boolean;
  status: "active" | "expired" | null;
  workspaceName: string | null;
  connectedAt: string | null;
  lastUsedAt: string | null;
}

export interface NotionConnectionPort {
  connection(): Promise<NotionConnection>;
  /** Notion's consent page; the browser returns to `returnTo` (a path of this site) with `?notion=<code>`. */
  authorize(returnTo: string): Promise<{ url: string }>;
  /** Forgets the caller's Notion connection and revokes the grant (best effort). */
  disconnect(): Promise<void>;
}
