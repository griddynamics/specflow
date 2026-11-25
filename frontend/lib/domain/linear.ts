// plans/LINEAR/LINEAR-PLAN.md: the caller's Linear connection (per-user OAuth, `actor=user`). FOUNDATION STUB: stream S1 fills the port. Screens
// reach it only through `LinearPort`; which project uses which member's connection is `IntegrationsPort` (`app: "linear"`).

export type LinearConnectionStatus = "none" | "active" | "expired" | "revoked";

/** `GET /integrations/linear`. `available` false = Linear is off on the server (everything else is empty). */
export interface LinearConnection {
  available: boolean;
  status: LinearConnectionStatus;
  userName: string | null;
  workspace: { id: string; name: string; urlKey: string } | null;
}

export interface LinearPort {
  connection(): Promise<LinearConnection>;
  /** The consent URL to send the browser to; `returnTo` is the same-site path the callback returns to. */
  authorize(returnTo: string): Promise<string>;
  disconnect(): Promise<void>;
}
