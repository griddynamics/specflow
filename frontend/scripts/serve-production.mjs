// FE-BP-03, FE-BP-09, FE-DOD-03 (TECH-SPECS §10.4; P17): serve the BUILT app on its PRODUCTION routes for the Playwright run
// `npm run e2e:production`, against the stub API (tests/support/stub-api.mjs), never the real backend: no model call is possible.
// Sign-in is the real Auth.js OIDC code flow against the stub issuer (tests/oidc/stub-issuer.mjs, RS256 JWTs) running in this process.
//   - frontend (standalone server.js): http://127.0.0.1:3260, APP_ENV=local, OIDC_ISSUER = the stub issuer's own address,
//     MCP_PUBLIC_URL=https://mcp.example.com/mcp, MCP_CLIENT_ID=specflow-test (the Guides snippets)
//   - stub API: http://127.0.0.1:3261 (the BFF's API_INTERNAL_URL; it verifies each bearer against the issuer's JWKS)
//   - stub issuer: http://127.0.0.1:3263/realms/stub (signs in prod.user@example.test unless the sign-in names another user)
//   - test control: http://127.0.0.1:3262  GET /log (method, path, content type, bytes, first bytes; never headers),
//                                          POST /timing (JSON: parseMs, summarizeMs, parseOutcome; null = Infinity) and the knowledge
//                                          build knobs: graphMs (null = Infinity: a build stays running), graphOutcome ("succeeded" |
//                                          "partially_succeeded" | "failed"), graphStartFail (null | an API code: the POST answers 503)
//                                          POST /seed-source ({pid, source}: a source with chosen file states; see stub-sources.mjs `seed`)
//                                          POST /seed-members ({name?, role?, members?, invites?, state?}: a project where the caller is owner or
//                                            collaborator, with chosen members and invites; `state` sets people knobs such as allowedDomains or
//                                            claimsReleased; answers the project; see stub-members.mjs `seed`)
//                                          POST /seed-jira ({pid, available?, target?, access?}: the Jira settings of a project. `available`
//                                            false = the server has no Atlassian account (503 jira_unavailable); `target` = {jira_project,
//                                            epic_key, enabled, created_epic_key?, sync?: JiraSyncStatus} or null (no target); `access` = "ok" or
//                                            {reason, field}, the answer of the next access checks; answers {}; the PUTs show in GET /log;
//                                            see stub-jira.mjs)
//                                          POST /remove-source ({pid, sid}: the source is gone, as if removed in another tab)
//                                          POST /seed-gaps ({pid, gaps, run, rules, corpus}: the gaps, the latest gap run, the noise rules
//                                            and the corpus documents of a project; stub-api.mjs `gapRoute`)
//                                          POST /settle-gaps ({pid}: the build of the applied answers finished; applying gaps resolve)
//                                          POST /record-gaps ({pid, gaps}: a running gap run records one more site: these gaps, and
//                                            its step_items_done counts the site; a gap with the same id is replaced)
//                                          GET /oidc (the issuer's observations, never a token: {endSession, endSessionCalls,
//                                            endSessionRequests: [{hasIdTokenHint, idTokenHintValid, postLogoutRedirectUri, clientId, ok}],
//                                            signIns, refreshCount})
//                                          POST /oidc/reset (clears the end-session calls and the other observations)
//                                          POST /oidc/end-session ({enabled}: false drops end_session_endpoint from discovery, as Google)
// Needs `npm run build` first. NEVER prints environment values or the generated secrets: it logs the URLs and "ready", passes
// the env to the child process only, and drops child output lines that contain a generated secret.
import http from "node:http";
import { spawnStandalone } from "./spawn-standalone.mjs";
import { frontendEnv, listen, prepareStandalone } from "../tests/support/servers.mjs";
import { startOidcStack } from "../tests/support/oidc-stack.mjs";

const PORT = 3260;
const API_PORT = 3261;
const CONTROL_PORT = 3262;
const ISSUER_PORT = 3263;

try {
  prepareStandalone(); // throws "run `npm run build` first" when the build is missing
} catch (e) {
  console.error(String(e.message));
  process.exit(2);
}

const stack = await startOidcStack({
  issuerPort: ISSUER_PORT,
  apiPort: API_PORT,
  defaultEmail: "prod.user@example.test",
  appOrigin: `http://127.0.0.1:${PORT}`,
  api: { parseMs: 1500, summarizeMs: 1500 },
});
const { api, issuer } = stack;

/** The issuer's observations without tokens: what a test may assert on (the end-session calls of a sign-out). */
const oidcView = () => ({
  endSession: issuer.endSession,
  endSessionCalls: issuer.endSessionRequests.length,
  endSessionRequests: issuer.endSessionRequests.map((r) => ({
    hasIdTokenHint: r.idTokenHint !== undefined,
    idTokenHintValid: r.idTokenHintValid,
    postLogoutRedirectUri: r.postLogoutRedirectUri,
    clientId: r.clientId,
    ok: r.ok,
  })),
  signIns: issuer.tokenRequests.filter((r) => r.grant_type === "authorization_code" && r.ok).length,
  refreshCount: issuer.refreshCount,
});

// Test control: the stub's request log without headers (they carry the bearer token) and its timing knobs.
const control = http.createServer(async (req, res) => {
  const send = (status, body) => {
    res.writeHead(status, { "content-type": "application/json" });
    res.end(JSON.stringify(body));
  };
  if (req.method === "GET" && req.url === "/log") {
    return send(
      200,
      api.log.map((e) => ({ method: e.method, path: e.path, contentType: e.contentType, bytes: e.bytes, head: e.head, authOk: e.authOk })),
    );
  }
  if (req.method === "POST" && req.url === "/timing") {
    const chunks = [];
    for await (const c of req) chunks.push(c);
    const t = JSON.parse(Buffer.concat(chunks).toString() || "{}");
    for (const k of ["parseMs", "summarizeMs", "graphMs"]) if (t[k] === null) t[k] = Infinity; // JSON has no Infinity: null keeps work running
    api.setTiming(t);
    return send(200, { ok: true });
  }
  if (req.method === "GET" && req.url === "/oidc") return send(200, oidcView());
  if (req.method === "POST" && req.url === "/oidc/reset") {
    for (const list of [issuer.endSessionRequests, issuer.tokenRequests, issuer.authorizeRequests]) list.length = 0;
    return send(200, { ok: true });
  }
  if (req.method === "POST" && req.url === "/oidc/end-session") {
    const chunks = [];
    for await (const c of req) chunks.push(c);
    let enabled;
    try {
      enabled = JSON.parse(Buffer.concat(chunks).toString() || "{}").enabled;
    } catch {
      enabled = undefined;
    }
    if (typeof enabled !== "boolean") return send(400, { error: "enabled_must_be_boolean" });
    issuer.endSession = enabled;
    return send(200, { endSession: issuer.endSession });
  }
  if (req.method === "POST" && req.url === "/seed-work") {
    const chunks = [];
    for await (const c of req) chunks.push(c);
    const { pid, work } = JSON.parse(Buffer.concat(chunks).toString() || "{}");
    api.seedWork(pid, work);
    return send(200, { ok: true });
  }
  if (req.method === "POST" && req.url === "/seed-gaps") {
    const chunks = [];
    for await (const c of req) chunks.push(c);
    const { pid, gaps, run, rules, corpus } = JSON.parse(Buffer.concat(chunks).toString() || "{}");
    api.seedGaps(pid, { gaps, run, rules, corpus });
    return send(200, { ok: true });
  }
  if (req.method === "POST" && req.url === "/settle-gaps") {
    const chunks = [];
    for await (const c of req) chunks.push(c);
    const { pid } = JSON.parse(Buffer.concat(chunks).toString() || "{}");
    api.settleGaps(pid);
    return send(200, { ok: true });
  }
  if (req.method === "POST" && req.url === "/record-gaps") {
    const chunks = [];
    for await (const c of req) chunks.push(c);
    const { pid, gaps } = JSON.parse(Buffer.concat(chunks).toString() || "{}");
    api.recordGaps(pid, gaps ?? []);
    return send(200, { ok: true });
  }
  if (req.method === "POST" && req.url === "/seed-members") {
    const chunks = [];
    for await (const c of req) chunks.push(c);
    let spec;
    try {
      spec = JSON.parse(Buffer.concat(chunks).toString() || "{}");
    } catch {
      return send(400, { error: "bad_json" });
    }
    const { name, state, ...rest } = spec;
    if (state) Object.assign(api.people.state, state);
    if (name === undefined) return send(200, { ok: true }); // knobs only: no project
    return send(200, api.seedProject(String(name ?? "Seeded project"), rest));
  }
  if (req.method === "POST" && req.url === "/seed-jira") {
    const chunks = [];
    for await (const c of req) chunks.push(c);
    let spec;
    try {
      spec = JSON.parse(Buffer.concat(chunks).toString() || "{}");
    } catch {
      return send(400, { error: "bad_json" });
    }
    api.jira.seed(spec);
    return send(200, {});
  }
  if (req.method === "POST" && (req.url === "/seed-source" || req.url === "/remove-source")) {
    const chunks = [];
    for await (const c of req) chunks.push(c);
    let parsed;
    try {
      parsed = JSON.parse(Buffer.concat(chunks).toString() || "{}");
    } catch {
      return send(400, { error: "bad_json" });
    }
    const { pid, source, sid } = parsed;
    if (req.url === "/seed-source") return send(200, { id: api.model.seed(pid, source) });
    api.model.remove(pid, sid);
    return send(200, { ok: true });
  }
  return send(404, { error: "not_found" });
});
await listen(control, CONTROL_PORT);

const env = frontendEnv(PORT, {
  APP_ENV: "local",
  ...stack.env,
  PUBLIC_URL: `http://127.0.0.1:${PORT}`,
  // The Guides and Plan › Develop build their snippets from these two (FR-MCP-UI-03); the specs read the same values.
  MCP_PUBLIC_URL: "https://mcp.example.com/mcp",
  MCP_CLIENT_ID: "specflow-test",
});

const child = spawnStandalone(env, stack.secrets);
async function shutdown(code) {
  child.kill("SIGTERM");
  control.close();
  await stack.close();
  process.exit(code);
}
child.on("exit", (code) => {
  console.log(`production server exited with ${code}`);
  void shutdown(code ?? 1);
});
for (const sig of ["SIGTERM", "SIGINT"]) process.on(sig, () => void shutdown(0));
console.log(
  `production server on http://127.0.0.1:${PORT} (stub API ${api.url}, stub issuer ${issuer.url}, control http://127.0.0.1:${CONTROL_PORT}; ready when /api/health answers)`,
);
