// FE-BP-03, FE-BP-09, FE-DOD-03 (TECH-SPECS §10.4; P17): serve the BUILT app on its PRODUCTION routes for the Playwright run
// `npm run e2e:production`, against the stub API (tests/support/stub-api.mjs), never the real backend: no model call is possible.
//   - frontend (standalone server.js): http://127.0.0.1:3260, APP_ENV=local, AUTH_MODE=dev (the dev sign-in form)
//   - stub API: http://127.0.0.1:3261 (the BFF's API_INTERNAL_URL; it verifies the dev token with the same random key)
//   - test control: http://127.0.0.1:3262  GET /log (method, path, content type, bytes, first bytes; never headers),
//                                          POST /timing (JSON: parseMs, summarizeMs, parseOutcome; null = Infinity)
//                                          POST /seed-source ({pid, source}: a source with chosen file states; see stub-sources.mjs `seed`)
//                                          POST /remove-source ({pid, sid}: the source is gone, as if removed in another tab)
// Needs `npm run build` first. NEVER prints environment values or the generated secrets: it logs the URLs and "ready", passes
// the env to the child process only, and drops child output lines that contain a generated secret.
import http from "node:http";
import { spawnStandalone } from "./spawn-standalone.mjs";
import { frontendEnv, listen, prepareStandalone, randomSecret } from "../tests/support/servers.mjs";
import { startStubApi } from "../tests/support/stub-api.mjs";

const PORT = 3260;
const API_PORT = 3261;
const CONTROL_PORT = 3262;
const secrets = [randomSecret(), randomSecret()];

try {
  prepareStandalone(); // throws "run `npm run build` first" when the build is missing
} catch (e) {
  console.error(String(e.message));
  process.exit(2);
}

const api = await startStubApi({ port: API_PORT, signingKey: secrets[1], parseMs: 1500, summarizeMs: 1500 });

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
    for (const k of ["parseMs", "summarizeMs"]) if (t[k] === null) t[k] = Infinity; // JSON has no Infinity: null keeps work running
    api.setTiming(t);
    return send(200, { ok: true });
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
  AUTH_MODE: "dev",
  SESSION_SECRET: secrets[0],
  AUTH_DEV_SIGNING_KEY: secrets[1],
  LOCAL_TEST_USER_EMAIL: "prod.user@example.test",
  API_INTERNAL_URL: api.url,
  PUBLIC_URL: `http://127.0.0.1:${PORT}`,
});

const child = spawnStandalone(env, secrets);
async function shutdown(code) {
  child.kill("SIGTERM");
  control.close();
  await api.close();
  process.exit(code);
}
child.on("exit", (code) => {
  console.log(`production server exited with ${code}`);
  void shutdown(code ?? 1);
});
for (const sig of ["SIGTERM", "SIGINT"]) process.on(sig, () => void shutdown(0));
console.log(
  `production server on http://127.0.0.1:${PORT} (stub API ${api.url}, control http://127.0.0.1:${CONTROL_PORT}; ready when /api/health answers)`,
);
