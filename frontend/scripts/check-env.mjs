// Startup gate: `node scripts/check-env.mjs` (AC-6.4, AC-2.6 frontend twin).
// Exit 0 on a valid env, 2 otherwise, with ONE line naming variables, never values.
// `--load-dotenv [--dev]` also reads frontend/.env* like Next does (host dev/start).
import { failureLine, validateEnv } from "../lib/env-rules.mjs";

if (process.argv.includes("--load-dotenv")) {
  const { default: nextEnv } = await import("@next/env");
  nextEnv.loadEnvConfig(process.cwd(), process.argv.includes("--dev"), {
    info: () => {},
    error: () => {},
  });
}

const variables = validateEnv(process.env);
if (variables.length > 0) {
  console.error(failureLine(variables));
  process.exit(2);
}
