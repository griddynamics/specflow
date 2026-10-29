import path from "node:path";
import type { ReporterDescription } from "@playwright/test";

/**
 * The reporters of a browser pack, writing under `out`.
 * `ci` is the `CI` environment variable, which the calling pack config reads: the config is the one module
 * of a pack that owns the environment.
 * Locally (`ci` unset, empty or "false"): the list in the terminal and an HTML report in `<out>/report`.
 * On CI (`ci` set and not "false", as GitHub Actions does): the list, `<out>/junit.xml` for the run summary
 * (validation/ci/ci_report.py) and a blob report per shard in `<out>/blob-report`, which the workflow merges into one HTML
 * report with the `merge-reports` command.
 */
export function packReporters(out: string, ci: string | undefined): ReporterDescription[] {
  if (ci === undefined || ci === "" || ci === "false") {
    return [["list"], ["html", { outputFolder: path.join(out, "report"), open: "never" }]];
  }
  return [
    ["list"],
    ["junit", { outputFile: path.join(out, "junit.xml"), includeProjectInTestName: true }],
    ["blob", { outputDir: path.join(out, "blob-report") }],
  ];
}
