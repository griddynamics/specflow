// Entry point of the clone check. The scan lives in duplicates-run.mjs and is loaded dynamically so
// that an unresolvable import or an uncaught error is a tool error (exit 2), never a clone finding
// (exit 1).
try {
  await import('./duplicates-run.mjs');
} catch (error) {
  process.stderr.write(`tool-error: duplicates check crashed: ${error?.stack ?? error}\n`);
  process.exit(2);
}
