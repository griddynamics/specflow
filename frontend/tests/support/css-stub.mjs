// Lets a node:test file import components that import `.css` (Next handles those in the build; node does not). Call before the imports.
// tsx runs the test files as CommonJS, so a `require` extension is all that is needed.
import { createRequire } from "node:module";

export function stubCss() {
  createRequire(import.meta.url).extensions[".css"] = () => {};
}
