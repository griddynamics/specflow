import fs from "node:fs";
import path from "node:path";
import { defineConfig, devices } from "@playwright/test";
import { packReporters } from "./playwright.reporter";

// Browser runs against the running compose stack (frontend `runtime` profile)
// (base URL from E2E_BASE_URL, default http://localhost:3001). Production build, headless by default.
// This live pack runs only on request, never in the validation workflow: it needs the whole stack and spends model budget.
// `E2E_ENV_FILE` (the stack's env file, e.g. `.env.local`) supplies the secrets the specs need (KEYCLOAK_QA_PASSWORD, ...):
// each KEY=VALUE line is read into the environment unless the shell already sets it. Nothing is printed.
function loadEnvFile(file: string | undefined): void {
  if (!file) return;
  for (const line of fs.readFileSync(file, "utf8").split("\n")) {
    const m = /^([A-Z0-9_]+)=(.*)$/.exec(line.trim());
    if (m && process.env[m[1]] === undefined) process.env[m[1]] = m[2].replace(/^["']|["']$/g, "");
  }
}
loadEnvFile(process.env.E2E_ENV_FILE);

const out = path.resolve(__dirname, process.env.E2E_OUT ?? "logs/e2e"); // `logs/` is git-ignored

export default defineConfig({
  testDir: "./e2e",
  outputDir: path.join(out, "results"),
  reporter: packReporters(out, process.env.CI), // under CI: the list, JUnit and a blob report; otherwise the list and an HTML report
  fullyParallel: false,
  workers: 1,
  retries: 0, // the agent spec must never retry: every run spends LLM gateway budget
  timeout: 5 * 60_000, // the hello spec raises its own limit (parse 10 min + summarize 15 min)
  use: {
    baseURL: process.env.E2E_BASE_URL ?? "http://localhost:3001",
    headless: true,
    trace: "on-first-retry",
    screenshot: "only-on-failure",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
});
