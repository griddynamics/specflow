// Stylelint config of the validation kit's lint track (validation/typescript/lint), run by `make validate-frontend`: the kit's
// generic config, plus one exemption. Paths are globs relative to this folder.
import { defineStylelintConfig } from "../validation/typescript/lint/stylelint.config.mjs";

const kit = defineStylelintConfig();

export default {
  ...kit,
  overrides: [
    ...kit.overrides,
    {
      // project: styles/ds/components/bundle.css is a byte copy of the read-only GD Liquid Glass design system (AGENTS.md;
      // pinned by a design-system copy check, FE-DS-04.AC1) and is already out of the Prettier and ESLint scans for the
      // same reason. Its gaps are covered, not hidden: styles/ds-a11y.css (imported right after it) switches off every
      // animation and transition for `prefers-reduced-motion: reduce` and restores the Modal focus ring, and
      // components/ui/ui-gaps.css rings the Menu items. Only the rules the frozen copy breaks are lifted, only for that file.
      files: ["styles/ds/components/bundle.css"],
      rules: {
        "a11y/reduced-motion": null,
        "declaration-property-value-disallowed-list": null,
        "no-duplicate-selectors": null,
      },
    },
  ],
};
