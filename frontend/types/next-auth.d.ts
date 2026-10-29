import "next-auth";
import "next-auth/jwt";

declare module "next-auth" {
  interface User {
    accessToken?: string;
    accessExpiresAt?: number;
  }
}

declare module "next-auth/jwt" {
  interface JWT {
    access_token?: string;
    refresh_token?: string;
    expires_at?: number;
  }
}
