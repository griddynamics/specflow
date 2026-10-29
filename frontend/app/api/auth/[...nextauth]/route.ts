import type { NextRequest } from "next/server";
import { handlers } from "@/auth";
import { appConfig } from "@/lib/config";

// Auth.js derives redirect and callback URLs from x-forwarded-host/proto (or Host).
// Behind a container or an ingress those can carry an internal host, so the auth
// endpoints (small bodies only) are pinned to PUBLIC_URL. The BFF is not touched:
// it never builds URLs from the request.
// A plain Request is built on purpose: NextRequest rewrites 127.0.0.1 to localhost.
async function pinned(req: NextRequest): Promise<NextRequest> {
  const { publicUrl, publicOrigin } = appConfig();
  const url = new URL(req.url);
  const target = new URL(publicUrl);
  const headers = new Headers(req.headers);
  headers.set("x-forwarded-host", target.host);
  headers.set("x-forwarded-proto", target.protocol.replace(":", ""));
  const hasBody = req.method !== "GET" && req.method !== "HEAD";
  return new Request(publicOrigin + url.pathname + url.search, {
    method: req.method,
    headers,
    body: hasBody ? await req.arrayBuffer() : undefined,
  }) as NextRequest;
}

export const GET = async (req: NextRequest) => handlers.GET(await pinned(req));
export const POST = async (req: NextRequest) => handlers.POST(await pinned(req));
