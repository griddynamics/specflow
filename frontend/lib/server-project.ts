// MCP-M10 (E11): server-only read of one project's name, for the plan page. It makes the call the BFF would make (the session's access
// token, `GET /api/v1/projects/{id}` on `API_INTERNAL_URL`), so the page can hand plain props to the client. Tokens stay on the server.
import { request as httpRequest } from "node:http";
import { request as httpsRequest } from "node:https";
import { headers } from "next/headers";
import { appConfig } from "@/lib/config";
import { accessTokenFor } from "@/lib/session-token";

interface Answer {
  status: number;
  body: string;
}

function get(url: URL, token: string): Promise<Answer | null> {
  const send = url.protocol === "https:" ? httpsRequest : httpRequest;
  return new Promise((resolve) => {
    const req = send(url, { method: "GET", headers: { authorization: `Bearer ${token}` }, timeout: 10_000 }, (res) => {
      const chunks: Buffer[] = [];
      res.on("data", (c: Buffer) => chunks.push(c));
      res.on("end", () => {
        resolve({ status: res.statusCode ?? 502, body: Buffer.concat(chunks).toString("utf8") });
      });
      res.on("error", () => {
        resolve(null);
      });
    });
    req.on("timeout", () => {
      req.destroy();
    });
    req.on("error", () => {
      resolve(null);
    });
    req.end();
  });
}

/** `{ name }` of the project, `"not_found"` when the API says so, or `null` when it could not be read (the page then still renders). */
export async function readProjectName(projectId: string): Promise<{ name: string } | "not_found" | null> {
  const cookie = (await headers()).get("cookie") ?? "";
  const token = await accessTokenFor(new Request("http://localhost/", { headers: { cookie } }));
  if (token === null || token === "") return null;
  const answer = await get(new URL(`/api/v1/projects/${encodeURIComponent(projectId)}`, appConfig().apiInternalUrl), token);
  if (answer === null) return null;
  if (answer.status === 404) return "not_found";
  if (answer.status !== 200) return null;
  try {
    const body: unknown = JSON.parse(answer.body);
    const name = typeof body === "object" && body !== null && "name" in body ? body.name : undefined;
    return typeof name === "string" ? { name } : null;
  } catch {
    return null;
  }
}
