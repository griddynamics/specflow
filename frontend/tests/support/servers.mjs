// Helpers to run the BUILT frontend (standalone server.js, what the runtime image runs)
// on ports 3200-3299. Never `next dev` (AC-12.4 measures production behaviour).
import { spawn, execFileSync } from "node:child_process";
import { cpSync, existsSync } from "node:fs";
import { randomBytes } from "node:crypto";
import path from "node:path";
import { fileURLToPath } from "node:url";

export const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
const standalone = path.join(root, ".next/standalone");

export const randomSecret = () => randomBytes(32).toString("hex");

export function prepareStandalone() {
  if (!existsSync(path.join(standalone, "server.js"))) {
    throw new Error("missing .next/standalone/server.js: run `npm run build` first");
  }
  // Same layout as the runtime image: static assets and public beside server.js.
  cpSync(path.join(root, ".next/static"), path.join(standalone, ".next/static"), { recursive: true, force: true });
  cpSync(path.join(root, "public"), path.join(standalone, "public"), { recursive: true, force: true });
}

/**
 * Env for a frontend process: only what is passed, never the caller's environment.
 * @param {number} port
 * @param {Record<string, string>} extra
 */
export function frontendEnv(port, extra) {
  return {
    PATH: process.env.PATH,
    NODE_ENV: "production",
    NEXT_TELEMETRY_DISABLED: "1",
    HOSTNAME: "127.0.0.1",
    PORT: String(port),
    PUBLIC_URL: `http://127.0.0.1:${port}`,
    ...extra,
  };
}

export async function startFrontend({ port, env, entry = "server.js", startTimeoutMs = 40_000 }) {
  prepareStandalone();
  const child = spawn(process.execPath, [entry], { cwd: standalone, env, stdio: ["ignore", "pipe", "pipe"] });
  let output = "";
  child.stdout.on("data", (d) => (output += d));
  child.stderr.on("data", (d) => (output += d));
  const exited = new Promise((resolve) => child.on("exit", (code) => resolve(code)));
  const base = `http://127.0.0.1:${port}`;
  const deadline = Date.now() + startTimeoutMs;
  for (;;) {
    if (child.exitCode !== null) throw new Error(`frontend exited with ${child.exitCode}: ${output.slice(-400)}`);
    try {
      const res = await fetch(`${base}/api/health`, { redirect: "manual" });
      if (res.ok) break;
    } catch {
      /* not up yet */
    }
    if (Date.now() > deadline) {
      child.kill("SIGKILL");
      throw new Error(`frontend did not start: ${output.slice(-400)}`);
    }
    await new Promise((r) => setTimeout(r, 200));
  }
  return {
    base,
    pid: child.pid,
    output: () => output,
    exited,
    async stop() {
      if (child.exitCode === null) child.kill("SIGTERM");
      const t = setTimeout(() => child.kill("SIGKILL"), 5000);
      await exited;
      clearTimeout(t);
    },
  };
}

/** RSS in KiB of a process, via ps (macOS and Linux). */
export function rssKiB(pid) {
  try {
    return (
      Number(
        execFileSync("ps", ["-o", "rss=", "-p", String(pid)])
          .toString()
          .trim(),
      ) || 0
    );
  } catch {
    return 0;
  }
}

export async function listen(server, port) {
  await new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(port, "127.0.0.1", resolve);
  });
}

export const close = (server) =>
  new Promise((resolve) => {
    server.closeAllConnections?.();
    server.close(() => resolve());
  });
