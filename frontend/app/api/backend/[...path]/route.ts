import { request as httpRequest, type IncomingHttpHeaders } from "node:http";
import { request as httpsRequest } from "node:https";
import { Readable } from "node:stream";
import { auth } from "@/auth";
import { appConfig } from "@/lib/config";
import { gcPacer } from "@/lib/gc-pacing";
import { accessTokenFor } from "@/lib/session-token";

// BFF (AC-6.6). The upload body is streamed with node:http + Readable.fromWeb().pipe():
// `fetch` has no backpressure to a slow upstream and a 300 s headers timeout (SP-C).
// Docs: backend-for-frontend.md "Proxying to a backend", route.md (params is a Promise).
export const dynamic = "force-dynamic";

const DROP_REQUEST = new Set([
  "cookie",
  "authorization",
  "host",
  "connection",
  "content-length",
  "transfer-encoding",
  "origin",
  "referer",
  "forwarded",
  "x-real-ip",
  "x-forwarded-for",
  "x-forwarded-host",
  "x-forwarded-proto",
  "x-forwarded-port",
  "keep-alive",
  "upgrade",
  "te",
  "expect",
  "x-http-method-override",
  "x-method-override",
  "x-original-url",
  "x-rewrite-url",
]);
const DROP_RESPONSE = new Set(["transfer-encoding", "connection", "keep-alive", "set-cookie"]);
const NULL_BODY = new Set([101, 204, 205, 304]);

const problem = (status: number, code: string) => Response.json({ error: { code, message: code, details: {} } }, { status });

/** `..` in any decoded form, or a segment that could change the path. */
function unsafePath(req: Request, segments: string[]): boolean {
  const raw = new URL(req.url).pathname.toLowerCase();
  // %25 catches double encoding (%252e%252e), which a second decode downstream would turn into `..`.
  if (raw.includes("%2e") || raw.includes("%2f") || raw.includes("%5c") || raw.includes("%25")) return true;
  return segments.some((s) => {
    let d: string;
    try {
      d = decodeURIComponent(s);
    } catch {
      return true;
    }
    return d === "" || d.includes("..") || d.includes("/") || d.includes("\\") || d.includes("\0");
  });
}

function upstreamHeaders(req: Request, token: string): Record<string, string> {
  const headers: Record<string, string> = {};
  req.headers.forEach((value, key) => {
    if (!DROP_REQUEST.has(key) && !key.startsWith("sec-")) headers[key] = value;
  });
  headers.authorization = `Bearer ${token}`;
  const length = req.headers.get("content-length");
  if (length !== null && length !== "") headers["content-length"] = length;
  return headers;
}

function responseHeaders(raw: IncomingHttpHeaders): Headers {
  const headers = new Headers();
  for (const [key, value] of Object.entries(raw)) {
    if (value === undefined || DROP_RESPONSE.has(key)) continue;
    headers.set(key, Array.isArray(value) ? value.join(", ") : value);
  }
  return headers;
}

function forward(req: Request, url: URL, headers: Record<string, string>): Promise<Response> {
  const send = url.protocol === "https:" ? httpsRequest : httpRequest;
  return new Promise<Response>((resolve) => {
    const up = send(url, { method: req.method, headers }, (res) => {
      const status = res.statusCode ?? 502;
      if (NULL_BODY.has(status)) {
        res.resume();
        resolve(new Response(null, { status, headers: responseHeaders(res.headers) }));
        return;
      }
      resolve(
        new Response(Readable.toWeb(res) as ReadableStream, {
          status,
          headers: responseHeaders(res.headers),
        }),
      );
    });
    up.on("error", () => {
      resolve(problem(502, "upstream_unavailable"));
    });
    // A client abort destroys the upstream request, so the API sees it and aborts its storage writer.
    req.signal.addEventListener("abort", () => up.destroy());
    if (req.body) {
      const pace = gcPacer();
      Readable.fromWeb(req.body as never)
        .on("data", (chunk: Buffer) => {
          pace(chunk.length);
        })
        .on("error", () => up.destroy())
        .pipe(up);
    } else {
      up.end();
    }
  });
}

/** The catch-all `path` segments of the route params; none when the params carry no string list. */
function pathSegments(params: unknown): string[] {
  if (typeof params !== "object" || params === null || !("path" in params)) return [];
  const { path } = params;
  return Array.isArray(path) ? path.filter((segment): segment is string => typeof segment === "string") : [];
}

const guarded = auth(async (req, ctx) => {
  const params: unknown = await ctx.params;
  const path = pathSegments(params);
  if (unsafePath(req, path)) return problem(400, "invalid_path");

  const cfg = appConfig();
  if (req.method !== "GET" && req.method !== "HEAD") {
    const originOk = req.headers.get("origin") === cfg.publicOrigin;
    if (!originOk || req.headers.get("x-requested-with") !== "specflow") {
      return problem(403, "csrf_rejected");
    }
  }
  if (!req.auth) return problem(401, "unauthenticated");
  const token = await accessTokenFor(req);
  if (token === null || token === "") return problem(401, "unauthenticated");

  const target = new URL(`/api/v1/${path.map(encodeURIComponent).join("/")}${new URL(req.url).search}`, cfg.apiInternalUrl);
  return forward(req, target, upstreamHeaders(req, token));
});

// `auth(fn)` returns a Promise of the wrapped handler when the config is lazy
// (auth.ts); the config is only built on the first request, never at build time.
type Req = Parameters<typeof guarded>[0];
type Ctx = Parameters<typeof guarded>[1];

async function proxyRequest(req: Req, ctx: Ctx): Promise<Response> {
  const response = await (await Promise.resolve(guarded))(req, ctx);
  return response instanceof Response ? response : problem(500, "internal_error");
}

export async function GET(req: Req, ctx: Ctx): Promise<Response> {
  return proxyRequest(req, ctx);
}
export async function POST(req: Req, ctx: Ctx): Promise<Response> {
  return proxyRequest(req, ctx);
}
export async function PUT(req: Req, ctx: Ctx): Promise<Response> {
  return proxyRequest(req, ctx);
}
export async function PATCH(req: Req, ctx: Ctx): Promise<Response> {
  return proxyRequest(req, ctx);
}
export async function DELETE(req: Req, ctx: Ctx): Promise<Response> {
  return proxyRequest(req, ctx);
}
