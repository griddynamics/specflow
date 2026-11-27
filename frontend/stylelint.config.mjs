// Stylelint config of the validation kit's lint track (validation/typescript/lint), run by `make validate-frontend`: the kit's
// generic config, unextended (Task 4 removed the frozen GD bundle and its exemption). Paths are globs relative to this folder.
import { defineStylelintConfig } from "../validation/typescript/lint/stylelint.config.mjs";

const kit = defineStylelintConfig();

export default {
  ...kit,
  // Generated output, git-ignored and out of the ESLint scan too (eslint.kit.config.mjs): the Next build, and the
  // Playwright reports and traces of `make e2e-production`, whose bundled third-party CSS is not this app's code.
  ignoreFiles: [".next/**", "playwright-report/**", "test-results/**", "logs/**"],
};
