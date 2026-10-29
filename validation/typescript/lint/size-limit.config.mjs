/**
 * Payload budgets for a built single-page application, measured by Size Limit (@size-limit/file,
 * Brotli): the JavaScript the entry document loads before anything renders, and all JavaScript
 * the build emits (code-split chunks included). The entry assets are read from the `index.html`
 * the browser receives, so the budget follows the build instead of a hand-kept file list.
 *
 * Run it after the build, from the kit folder (Size Limit finds @size-limit/file from the
 * package.json above its working directory, and the project need not declare it):
 *
 *   cd <kit> && SIZE_LIMIT_DIST=/path/to/project/dist npx size-limit --config size-limit.config.mjs
 *
 * or let the orchestrator do it: node <kit>/run.mjs --project <dir> --only size-limit --size-limit
 *
 * Settings (environment variables; the defaults are starting points to tune per project):
 *   SIZE_LIMIT_DIST         the build output directory          (default: dist)
 *   SIZE_LIMIT_ENTRY_JS_B   entry JavaScript budget, in bytes   (default: 180000)
 *   SIZE_LIMIT_TOTAL_JS_B   total JavaScript budget, in bytes   (default: 450000)
 */
import { readFileSync } from 'node:fs';
import path from 'node:path';

import { budget, entryAssetPaths, totalJavaScriptPaths } from './size-limit/assets.mjs';

const DIST = path.resolve(process.env['SIZE_LIMIT_DIST'] ?? 'dist');

export default [
  {
    name: 'entry JavaScript (Brotli)',
    path: entryAssetPaths(readFileSync(path.join(DIST, 'index.html'), 'utf8'), DIST),
    limit: budget(process.env, 'SIZE_LIMIT_ENTRY_JS_B', 180000),
  },
  {
    name: 'total JavaScript (Brotli)',
    path: totalJavaScriptPaths(DIST),
    limit: budget(process.env, 'SIZE_LIMIT_TOTAL_JS_B', 450000),
  },
];
