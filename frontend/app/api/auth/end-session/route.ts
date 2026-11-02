import { endSessionUrlFor } from "@/lib/end-session";

// RP-initiated logout, step 1 (production-shell.tsx): answers where the browser goes after the local sign-out. A static segment, so it
// is matched ahead of the `[...nextauth]` catch-all next to it. POST only (it is called by the page, never navigated to) and never cached:
// the answer can carry the user's id_token.
export async function POST(req: Request) {
  const url = await endSessionUrlFor(req);
  return Response.json({ url }, { headers: { "cache-control": "no-store" } });
}
