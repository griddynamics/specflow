// Defense in depth next to scripts/check-env.mjs: a bad env stops the server at
// startup (AC-6.4). Skipped at build time, where runtime env is not present.
export async function register() {
  if (process.env.NEXT_RUNTIME !== "nodejs") return;
  if (process.env.NEXT_PHASE === "phase-production-build") return;
  const { validateEnv } = await import("./lib/env-rules.mjs");
  const variables = validateEnv(process.env);
  if (variables.length > 0) {
    throw new Error(`config.validation.failed variables=${variables.join(",")}`);
  }
}
