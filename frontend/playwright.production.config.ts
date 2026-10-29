import path from "node:path";
import { defineConfig, devices } from "@playwright/test";
import { packReporters } from "./playwright.reporter";

// P17 (FE-BP-03, FE-BP-09, TECH-SPECS §10.4): browser runs for the PRODUCTION routes (`/`, `/login`, `/projects/...`, `/connections`)
// against the stub API, never the real backend: no model call. It starts the BUILT app through scripts/serve-production.mjs
// (dev sign-in, stub API, control port), so run `npm run build` first. The specs share one stub, so they run one at a time
// (workers 1), in both viewports, with no retries.
const out = path.resolve(__dirname, "logs/e2e-production"); // `logs/` is git-ignored
const baseURL = process.env.E2E_PRODUCTION_BASE_URL ?? "http://127.0.0.1:3260";

export default defineConfig({
  testDir: "./e2e-production",
  outputDir: path.join(out, "results"),
  reporter: packReporters(out, process.env.CI), // under CI: the list, JUnit and a blob report per shard; otherwise the list and an HTML report
  fullyParallel: false,
  workers: 1,
  retries: 0,
  timeout: 60_000,
  expect: { timeout: 15_000 },
  use: { baseURL, headless: true, trace: "retain-on-failure", screenshot: "only-on-failure" },
  projects: [
    { name: "chromium-1280", use: { ...devices["Desktop Chrome"], viewport: { width: 1280, height: 800 } } },
    { name: "chromium-1920", use: { ...devices["Desktop Chrome"], viewport: { width: 1920, height: 1080 } } },
  ],
  webServer:
    process.env.E2E_PRODUCTION_BASE_URL !== undefined && process.env.E2E_PRODUCTION_BASE_URL !== ""
      ? undefined
      : {
          command: "node scripts/serve-production.mjs",
          url: "http://127.0.0.1:3260/api/health",
          reuseExistingServer: false,
          timeout: 60_000,
        },
});
