import "next-auth/jwt";

declare module "next-auth/jwt" {
  interface JWT {
    access_token?: string;
    refresh_token?: string;
    /** Server-side only: the logout hint. It reaches the browser only inside the end-session redirect URL. */
    id_token?: string;
    expires_at?: number;
  }
}
