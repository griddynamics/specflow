// Used by serve-production.mjs: start the BUILT app (standalone server.js) with the given env and forward its
// output, dropping every line that contains a generated secret.
import { spawn } from "node:child_process";
import path from "node:path";
import readline from "node:readline";
import { fileURLToPath } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
// The standalone tree is rooted at the workspace root (next.config.ts outputFileTracingRoot): the server is frontend/server.js.

export function spawnStandalone(env, secrets) {
  const child = spawn(process.execPath, ["server.js"], {
    cwd: path.join(root, ".next/standalone/frontend"),
    env,
    stdio: ["ignore", "pipe", "pipe"],
  });
  for (const stream of [child.stdout, child.stderr]) {
    readline.createInterface({ input: stream }).on("line", (line) => {
      if (!secrets.some((s) => line.includes(s))) console.log(line);
    });
  }
  return child;
}
